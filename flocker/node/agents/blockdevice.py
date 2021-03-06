# -*- test-case-name: flocker.node.agents.test.test_blockdevice -*-
# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
This module implements the parts of a block-device based dataset
convergence agent that can be re-used against many different kinds of block
devices.
"""

from uuid import UUID
from subprocess import check_output
from functools import wraps

from eliot import ActionType, Field, Logger
from eliot.serializers import identity

from zope.interface import implementer, Interface

from pyrsistent import PRecord, field

import psutil

from twisted.internet.defer import succeed
from twisted.python.filepath import FilePath

from .. import IDeployer, IStateChange, Sequentially, InParallel
from ...control import NodeState, Manifestation, Dataset, NonManifestDatasets

# Eliot is transitioning away from the "Logger instances all over the place"
# approach.  And it's hard to put Logger instances on PRecord subclasses which
# we have a lot of.  So just use this global logger for now.
_logger = Logger()


class VolumeException(Exception):
    """
    A base class for exceptions raised by  ``IBlockDeviceAPI`` operations.

    :param unicode blockdevice_id: The unique identifier of the block device.
    """
    def __init__(self, blockdevice_id):
        if not isinstance(blockdevice_id, unicode):
            raise TypeError(
                'Unexpected blockdevice_id type. '
                'Expected unicode. '
                'Got {!r}.'.format(blockdevice_id)
            )
        Exception.__init__(self, blockdevice_id)
        self.blockdevice_id = blockdevice_id


class UnknownVolume(VolumeException):
    """
    The block device could not be found.
    """


class AlreadyAttachedVolume(VolumeException):
    """
    A failed attempt to attach a block device that is already attached.
    """


class UnattachedVolume(VolumeException):
    """
    An attempt was made to operate on an unattached volume but the operation
    requires the volume to be attached.
    """


DATASET = Field(
    u"dataset",
    lambda dataset: dataset.dataset_id,
    u"The unique identifier of a dataset."
)

VOLUME = Field(
    u"volume",
    lambda volume: volume.blockdevice_id,
    u"The unique identifier of a volume."
)

DATASET_ID = Field(
    u"dataset_id",
    lambda dataset_id: unicode(dataset_id),
    u"The unique identifier of a dataset."
)

MOUNTPOINT = Field(
    u"mountpoint",
    lambda path: path.path,
    u"The absolute path to the location on the node where the dataset will be "
    u"mounted.",
)

DEVICE_PATH = Field(
    u"block_device_path",
    lambda path: path.path,
    u"The absolute path to the block device file on the node where the "
    u"dataset is attached.",
)

BLOCK_DEVICE_ID = Field(
    u"block_device_id",
    lambda id: unicode(id),
    u"The unique identifier if the underlying block device."
)

BLOCK_DEVICE_SIZE = Field(
    u"block_device_size",
    identity,
    u"The size of the underlying block device."
)

BLOCK_DEVICE_HOST = Field(
    u"block_device_host",
    identity,
    u"The host to which the underlying block device is attached."
)

CREATE_BLOCK_DEVICE_DATASET = ActionType(
    u"agent:blockdevice:create",
    [DATASET, MOUNTPOINT],
    [DEVICE_PATH, BLOCK_DEVICE_ID, DATASET_ID, BLOCK_DEVICE_SIZE,
     BLOCK_DEVICE_HOST],
    u"A block-device-backed dataset is being created.",
)

DESTROY_BLOCK_DEVICE_DATASET = ActionType(
    u"agent:blockdevice:destroy",
    [DATASET_ID],
    [],
    u"A block-device-backed dataset is being destroyed.",
)

UNMOUNT_BLOCK_DEVICE = ActionType(
    u"agent:blockdevice:unmount",
    [VOLUME],
    [],
    u"A block-device-backed dataset is being unmounted.",
)

DETACH_VOLUME = ActionType(
    u"agent:blockdevice:detach_volume",
    [VOLUME],
    [],
    u"The volume for a block-device-backed dataset is being detached."
)

DESTROY_VOLUME = ActionType(
    u"agent:blockdevice:destroy_volume",
    [VOLUME],
    [],
    u"The volume for a block-device-backed dataset is being destroyed."
)


def _logged_statechange(cls):
    """
    Decorate an ``IStateChange.run`` implementation with partially automatic
    logging.

    :param cls: An ``IStateChange`` implementation which also has an
        ``_eliot_action`` attribute giving an Eliot action that should be used
        to log its ``run`` method.

    :return: ``cls``, mutated so that its ``run`` method is automatically run
        in the context of its ``_eliot_action``.
    """
    original_run = cls.run
    # Work-around https://twistedmatrix.com/trac/ticket/7832
    try:
        original_run.__name__ = original_run.methodName
    except AttributeError:
        pass

    @wraps(original_run)
    def run(self, deployer):
        with self._eliot_action:
            # IStateChange.run nominally returns a Deferred.  Hook it up to the
            # action properly.  Do this as part of FLOC-1549 or maybe earlier.
            return original_run(self, deployer)

    cls.run = run
    return cls


class BlockDeviceVolume(PRecord):
    """
    A block device that may be attached to a host.

    :ivar unicode blockdevice_id: An identifier for the block device which is
        unique across the entire cluster.  For example, an EBS volume
        identifier (``vol-4282672b``).  This is used to address the block
        device for operations like attach and detach.
    :ivar int size: The size, in bytes, of the block device.
    :ivar unicode host: The IP address of the host to which the block device is
        attached or ``None`` if it is currently unattached.
    :ivar UUID dataset_id: The Flocker dataset ID associated with this volume.
    """
    blockdevice_id = field(type=unicode, mandatory=True)
    size = field(type=int, mandatory=True)
    host = field(type=(unicode, type(None)), initial=None)
    dataset_id = field(type=UUID, mandatory=True)


# Replace this with a simpler factory-function based API like:
#
#     change = destroy_blockdevice_dataset(volme)
#
# after FLOC-1591 makes it possible to have reasonable logging with such a
# solution.
@_logged_statechange
@implementer(IStateChange)
class DestroyBlockDeviceDataset(PRecord):
    """
    Destroy the volume for a dataset with a primary manifestation on the node
    where this state change runs.

    :ivar UUID dataset_id: The unique identifier of the dataset to which the
        volume to be destroyed belongs.
    """
    dataset_id = field(type=UUID, mandatory=True)

    @property
    def _eliot_action(self):
        return DESTROY_BLOCK_DEVICE_DATASET(
            _logger, dataset_id=self.dataset_id
        )

    def run(self, deployer):
        for volume in deployer.block_device_api.list_volumes():
            if volume.dataset_id == self.dataset_id:
                return Sequentially(
                    changes=[
                        UnmountBlockDevice(volume=volume),
                        DetachVolume(volume=volume),
                        DestroyVolume(volume=volume),
                    ]
                ).run(deployer)
        return succeed(None)


def _volume():
    """
    Create and return a ``PRecord`` ``field`` to hold a ``BlockDeviceVolume``.
    """
    return field(
        type=BlockDeviceVolume, mandatory=True,
        # Disable the automatic PRecord.create factory.  Callers can just
        # supply the right type, we don't need the magic coercion behavior
        # supplied by default.
        factory=lambda x: x
    )


@_logged_statechange
@implementer(IStateChange)
class UnmountBlockDevice(PRecord):
    """
    Unmount the filesystem mounted from the block device backed by a particular
    volume.

    :ivar BlockDeviceVolume volume: The volume associated with the dataset
        which will be unmounted.
    """
    volume = _volume()

    @property
    def _eliot_action(self):
        return UNMOUNT_BLOCK_DEVICE(_logger, volume=self.volume)

    def run(self, deployer):
        """
        Run the system ``unmount`` tool to unmount this change's volume's block
        device.  The volume must be attached to this node and the corresponding
        block device mounted.
        """
        device = deployer.block_device_api.get_device_path(
            self.volume.blockdevice_id
        )
        # This should be asynchronous.  Do it as part of FLOC-1499.  Make sure
        # to fix _logged_statechange to handle Deferreds too.
        check_output([b"umount", device.path])
        return succeed(None)


@_logged_statechange
@implementer(IStateChange)
class DetachVolume(PRecord):
    """
    Detach a volume from the node it is currently attached to.

    :ivar BlockDeviceVolume volume: The volume to destroy.
    """
    volume = _volume()

    @property
    def _eliot_action(self):
        return DETACH_VOLUME(_logger, volume=self.volume)

    def run(self, deployer):
        """
        Use the deployer's ``IBlockDeviceAPI`` to detach the volume.
        """
        # Make this asynchronous after FLOC-1549, probably as part of
        # FLOC-1593.
        deployer.block_device_api.detach_volume(self.volume.blockdevice_id)
        return succeed(None)


@_logged_statechange
@implementer(IStateChange)
class DestroyVolume(PRecord):
    """
    Destroy the storage (and therefore contents) of a volume.

    :ivar BlockDeviceVolume volume: The volume to destroy.
    """
    volume = _volume()

    @property
    def _eliot_action(self):
        return DESTROY_VOLUME(_logger, volume=self.volume)

    def run(self, deployer):
        """
        Use the deployer's ``IBlockDeviceAPI`` to destroy the volume.
        """
        # Make this asynchronous as part of FLOC-1549.
        deployer.block_device_api.destroy_volume(self.volume.blockdevice_id)
        return succeed(None)


@implementer(IStateChange)
class CreateBlockDeviceDataset(PRecord):
    """
    An operation to create a new dataset on a newly created volume with a newly
    initialized filesystem.

    :ivar Dataset dataset: The dataset for which to create a block device.
    :ivar FilePath mountpoint: The path at which to mount the created device.
    """
    dataset = field(mandatory=True, type=Dataset)
    mountpoint = field(mandatory=True, type=FilePath)

    def run(self, deployer):
        """
        Create a block device, attach it to the local host, create an ``ext4``
        filesystem on the device and mount it.

        Operations are performed synchronously.

        See ``IStateChange.run`` for general argument and return type
        documentation.

        :returns: An already fired ``Deferred`` with result ``None``.
        """
        with CREATE_BLOCK_DEVICE_DATASET(
                _logger,
                dataset=self.dataset, mountpoint=self.mountpoint
        ) as action:
            api = deployer.block_device_api
            volume = api.create_volume(
                dataset_id=UUID(self.dataset.dataset_id),
                size=self.dataset.maximum_size,
            )

            # This will be factored into a separate IStateChange to support the
            # case where the volume exists but is not attached.  That object
            # will be used by this one to perform this work.  FLOC-1575
            volume = api.attach_volume(
                volume.blockdevice_id, deployer.hostname
            )
            device = api.get_device_path(volume.blockdevice_id)

            # This will be factored into a separate IStateChange to support the
            # case where the volume is attached but has no filesystem.  That
            # object will be used by this one to perform this work. FLOC-1576
            check_output(["mkfs", "-t", "ext4", device.path])

            # This will be factored into a separate IStateChange to support the
            # case where the only state change necessary is mounting.  That
            # object will be used by this one to perform this mount. It will
            # also gracefully handle the case where the mountpoint directory
            # already exists.  FLOC-1498
            self.mountpoint.makedirs()
            check_output(["mount", device.path, self.mountpoint.path])

            action.add_success_fields(
                block_device_path=device,
                block_device_id=volume.blockdevice_id,
                dataset_id=volume.dataset_id,
                block_device_size=volume.size,
                block_device_host=volume.host,
            )
        return succeed(None)


# TODO: Introduce a non-blocking version of this interface and an automatic
# thread-based wrapper for adapting this to the other.  Use that interface
# anywhere being non-blocking is important (which is probably lots of places).
# See https://clusterhq.atlassian.net/browse/FLOC-1549
class IBlockDeviceAPI(Interface):
    """
    Common operations provided by all block device backends.

    Note: This is an early sketch of the interface and it'll be refined as we
    real blockdevice providers are implemented.
    """
    def create_volume(dataset_id, size):
        """
        Create a new volume.

        XXX: Probably needs to be some checking of valid sizes for different
        backends. Perhaps the allowed sizes should be defined as constants?

        :param UUID dataset_id: The Flocker dataset ID of the dataset on this
            volume.
        :param int size: The size of the new volume in bytes.
        :returns: A ``BlockDeviceVolume``.
        """

    def destroy_volume(blockdevice_id):
        """
        Destroy an existing volume.

        :param unicode blockdevice_id: The unique identifier for the volume to
            destroy.

        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.

        :return: ``None``
        """

    def attach_volume(blockdevice_id, host):
        """
        Attach ``blockdevice_id`` to ``host``.

        :param unicode blockdevice_id: The unique identifier for the block
            device being attached.
        :param unicode host: The IP address of a host to attach the volume to.
        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.
        :raises AlreadyAttachedVolume: If the supplied ``blockdevice_id`` is
            already attached.
        :returns: A ``BlockDeviceVolume`` with a ``host`` attribute set to
            ``host``.
        """

    def detach_volume(blockdevice_id):
        """
        Detach ``blockdevice_id`` from whatever host it is attached to.

        :param unicode blockdevice_id: The unique identifier for the block
            device being detached.

        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.
        :raises UnattachedVolume: If the supplied ``blockdevice_id`` is
            not attached to anything.
        :returns: ``None``
        """

    def list_volumes():
        """
        List all the block devices available via the back end API.

        :returns: A ``list`` of ``BlockDeviceVolume``s.
        """

    def get_device_path(blockdevice_id):
        """
        Return the device path that has been allocated to the block device on
        the host to which it is currently attached.

        :param unicode blockdevice_id: The unique identifier for the block
            device.
        :raises UnknownVolume: If the supplied ``blockdevice_id`` does not
            exist.
        :raises UnattachedVolume: If the supplied ``blockdevice_id`` is
            not attached to a host.
        :returns: A ``FilePath`` for the device.
        """


def _blockdevicevolume_from_dataset_id(dataset_id, size, host=None):
    """
    Create a new ``BlockDeviceVolume`` with a ``blockdevice_id`` derived
    from the given ``dataset_id``.

    This is for convenience of implementation of the loopback backend (to
    avoid needing a separate data store for mapping dataset ids to block
    device ids and back again).

    Parameters accepted have the same meaning as the attributes of
    ``BlockDeviceVolume``.
    """
    return BlockDeviceVolume(
        size=size, host=host, dataset_id=dataset_id,
        blockdevice_id=u"block-{0}".format(dataset_id),
    )


def _blockdevicevolume_from_blockdevice_id(blockdevice_id, size, host=None):
    """
    Create a new ``BlockDeviceVolume`` with a ``dataset_id`` derived from
    the given ``blockdevice_id``.

    This reverses the transformation performed by
    ``_blockdevicevolume_from_dataset_id``.

    Parameters accepted have the same meaning as the attributes of
    ``BlockDeviceVolume``.
    """
    # Strip the "block-" prefix we added.
    dataset_id = UUID(blockdevice_id[6:])
    return BlockDeviceVolume(
        size=size, host=host, dataset_id=dataset_id,
        blockdevice_id=blockdevice_id,
    )


def _losetup_list_parse(output):
    """
    Parse the output of ``losetup --all`` which varies depending on the
    privileges of the user.

    :param unicode output: The output of ``losetup --all``.
    :returns: A ``list`` of
        2-tuple(FilePath(device_file), FilePath(backing_file))
    """
    devices = []
    for line in output.splitlines():
        parts = line.split(u":", 2)
        if len(parts) != 3:
            continue
        device_file, attributes, backing_file = parts
        device_file = FilePath(device_file.strip().encode("utf-8"))

        # Trim everything from the first left bracket, skipping over the
        # possible inode number which appears only when run as root.
        left_bracket_offset = backing_file.find(b"(")
        backing_file = backing_file[left_bracket_offset + 1:]

        # Trim everything from the right most right bracket
        right_bracket_offset = backing_file.rfind(b")")
        backing_file = backing_file[:right_bracket_offset]

        # Trim a possible embedded deleted flag
        expected_suffix_list = [b"(deleted)"]
        for suffix in expected_suffix_list:
            offset = backing_file.rfind(suffix)
            if offset > -1:
                backing_file = backing_file[:offset]

        # Remove the space that may have been between the path and the deleted
        # flag.
        backing_file = backing_file.rstrip()
        backing_file = FilePath(backing_file.encode("utf-8"))
        devices.append((device_file, backing_file))
    return devices


def _losetup_list():
    """
    List all the loopback devices on the system.

    :returns: A ``list`` of
        2-tuple(FilePath(device_file), FilePath(backing_file))
    """
    output = check_output(
        ["losetup", "--all"]
    ).decode('utf8')
    return _losetup_list_parse(output)


def _device_for_path(expected_backing_file):
    """
    :param FilePath backing_file: A path which may be associated with a
        loopback device.
    :returns: A ``FilePath`` to the loopback device if one is found, or
        ``None`` if no device exists.
    """
    for device_file, backing_file in _losetup_list():
        if expected_backing_file == backing_file:
            return device_file


@implementer(IBlockDeviceAPI)
class LoopbackBlockDeviceAPI(object):
    """
    A simulated ``IBlockDeviceAPI`` which creates loopback devices backed by
    files located beneath the supplied ``root_path``.
    """
    _attached_directory_name = 'attached'
    _unattached_directory_name = 'unattached'

    def __init__(self, root_path):
        """
        :param FilePath root_path: The path beneath which all loopback backing
            files and their organising directories will be created.
        """
        self._root_path = root_path

    @classmethod
    def from_path(cls, root_path):
        """
        :param bytes root_path: The path to a directory in which loop back
            backing files will be created. The directory is created if it does
            not already exist.
        :returns: A ``LoopbackBlockDeviceAPI`` with the supplied ``root_path``.
        """
        api = cls(root_path=FilePath(root_path))
        api._initialise_directories()
        return api

    def _initialise_directories(self):
        """
        Create the root and sub-directories in which loopback files will be
        created.
        """
        self._unattached_directory = self._root_path.child(
            self._unattached_directory_name)

        try:
            self._unattached_directory.makedirs()
        except OSError:
            pass

        self._attached_directory = self._root_path.child(
            self._attached_directory_name)

        try:
            self._attached_directory.makedirs()
        except OSError:
            pass

    def create_volume(self, dataset_id, size):
        """
        Create a "sparse" file of some size and put it in the ``unattached``
        directory.

        See ``IBlockDeviceAPI.create_volume`` for parameter and return type
        documentation.
        """
        volume = _blockdevicevolume_from_dataset_id(
            size=size, dataset_id=dataset_id,
        )
        with self._unattached_directory.child(
            volume.blockdevice_id.encode('ascii')
        ).open('wb') as f:
            f.truncate(size)
        return volume

    def destroy_volume(self, blockdevice_id):
        """
        Destroy the storage for the given unattached volume.
        """
        volume = self._get(blockdevice_id)
        volume_path = self._unattached_directory.child(
            volume.blockdevice_id.encode("ascii")
        )
        volume_path.remove()

    def _get(self, blockdevice_id):
        for volume in self.list_volumes():
            if volume.blockdevice_id == blockdevice_id:
                return volume
        raise UnknownVolume(blockdevice_id)

    def attach_volume(self, blockdevice_id, host):
        """
        Move an existing ``unattached`` file into a per-host directory and
        create a loopback device backed by that file.

        Note: Although `mkfs` can format files directly and `mount` can mount
        files directly (with the `-o loop` option), we want to simulate a real
        block device which will be allocated a real block device file on the
        host to which it is attached. This allows the consumer of this API to
        perform formatting and mount operations exactly the same as for a real
        block device.

        See ``IBlockDeviceAPI.attach_volume`` for parameter and return type
        documentation.
        """
        volume = self._get(blockdevice_id)
        if volume.host is None:
            old_path = self._unattached_directory.child(blockdevice_id)
            host_directory = self._attached_directory.child(
                host.encode("utf-8")
            )
            try:
                host_directory.makedirs()
            except OSError:
                pass
            new_path = host_directory.child(blockdevice_id)
            old_path.moveTo(new_path)
            # The --find option allocates the next available /dev/loopX device
            # name to the device.
            check_output(["losetup", "--find", new_path.path])
            attached_volume = volume.set(host=host)
            return attached_volume

        raise AlreadyAttachedVolume(blockdevice_id)

    def detach_volume(self, blockdevice_id):
        """
        Move an existing file from a per-host directory into the ``unattached``
        directory and release the loopback device backed by that file.
        """
        volume = self._get(blockdevice_id)
        if volume.host is None:
            raise UnattachedVolume(blockdevice_id)

        check_output([
            b"losetup", b"--detach", self.get_device_path(blockdevice_id).path
        ])
        volume_path = self._attached_directory.descendant([
            volume.host.encode("ascii"), volume.blockdevice_id.encode("ascii")
        ])
        new_path = self._unattached_directory.child(
            volume.blockdevice_id.encode("ascii")
        )
        volume_path.moveTo(new_path)

    def list_volumes(self):
        """
        Return ``BlockDeviceVolume`` instances for all the files in the
        ``unattached`` directory and all per-host directories.

        See ``IBlockDeviceAPI.list_volumes`` for parameter and return type
        documentation.
        """
        volumes = []
        for child in self._root_path.child('unattached').children():
            blockdevice_id = child.basename().decode('ascii')
            volume = _blockdevicevolume_from_blockdevice_id(
                blockdevice_id=blockdevice_id,
                size=child.getsize(),
            )
            volumes.append(volume)

        for host_directory in self._root_path.child('attached').children():
            host_name = host_directory.basename().decode('ascii')
            for child in host_directory.children():
                blockdevice_id = child.basename().decode('ascii')
                volume = _blockdevicevolume_from_blockdevice_id(
                    blockdevice_id=blockdevice_id,
                    size=child.getsize(),
                    host=host_name,
                )
                volumes.append(volume)

        return volumes

    def get_device_path(self, blockdevice_id):
        volume = self._get(blockdevice_id)
        if volume.host is None:
            raise UnattachedVolume(blockdevice_id)

        volume_path = self._attached_directory.descendant(
            [volume.host.encode("ascii"),
             volume.blockdevice_id.encode("ascii")]
        )
        # May be None if the file hasn't been used for a loop device.
        return _device_for_path(volume_path)


def _manifestation_from_volume(volume):
    """
    :param BlockDeviceVolume volume: The block device which has the
        manifestation of a dataset.
    :returns: A primary ``Manifestation`` of a ``Dataset`` with the same id as
        the supplied ``BlockDeviceVolume``.
    """
    dataset = Dataset(
        dataset_id=volume.dataset_id,
        maximum_size=volume.size,
    )
    return Manifestation(dataset=dataset, primary=True)


@implementer(IDeployer)
class BlockDeviceDeployer(PRecord):
    """
    An ``IDeployer`` that operates on ``IBlockDeviceAPI`` providers.

    :ivar unicode hostname: The IP address of the node that has this deployer.
    :ivar IBlockDeviceAPI block_device_api: The block device API that will be
        called upon to perform block device operations.
    :ivar FilePath mountroot: The directory where block devices will be
        mounted.
    """
    hostname = field(type=unicode, mandatory=True)
    block_device_api = field(mandatory=True)
    mountroot = field(type=FilePath, initial=FilePath(b"/flocker"))

    def _get_system_mounts(self, volumes):
        """
        Load information about mounted filesystems related to the given
        volumes.

        :param list volumes: The ``BlockDeviceVolumes`` known to exist.  They
            may or may not be attached to this host.  Only system mounts that
            related to these volumes will be returned.

        :return: A ``dict`` mapping mount points (directories represented using
            ``FilePath``) to dataset identifiers (as ``UUID``\ s) representing
            all of the mounts on this system that were discovered and related
            to ``volumes``.
        """
        partitions = psutil.disk_partitions()
        device_to_dataset_id = {
            self.block_device_api.get_device_path(volume.blockdevice_id):
                volume.dataset_id
            for volume
            in volumes
            if volume.host == self.hostname
        }
        return {
            FilePath(partition.mountpoint):
                device_to_dataset_id[FilePath(partition.device)]
            for partition
            in partitions
            if FilePath(partition.device) in device_to_dataset_id
        }

    def discover_state(self, node_state):
        """
        Find all block devices that are currently associated with this host and
        return a ``NodeState`` containing only ``Manifestation`` instances and
        their mount paths.
        """
        volumes = self.block_device_api.list_volumes()
        manifestations = {}
        nonmanifest = {}

        for volume in volumes:
            dataset_id = unicode(volume.dataset_id)
            if volume.host == self.hostname:
                manifestations[dataset_id] = _manifestation_from_volume(
                    volume
                )
            elif volume.host is None:
                nonmanifest[dataset_id] = Dataset(dataset_id=dataset_id)

        system_mounts = self._get_system_mounts(volumes)

        paths = {}
        for manifestation in manifestations.values():
            dataset_id = manifestation.dataset.dataset_id
            mountpath = self._mountpath_for_manifestation(manifestation)

            # If the expected mount point doesn't actually have the device
            # mounted where we expected to find this manifestation, the
            # manifestation doesn't really exist here.
            properly_mounted = system_mounts.get(mountpath) == UUID(dataset_id)

            # In the future it would be nice to be able to represent
            # intermediate states (at least internally, if not exposed via the
            # API).  This makes certain IStateChange implementations easier
            # (for example, we could know something is attached and has a
            # filesystem, so we can just mount it - instead of doing something
            # about those first two state changes like trying them and handling
            # failure or doing more system inspection to try to see what's up).
            # But ... the future.

            if properly_mounted:
                paths[dataset_id] = mountpath
            else:
                del manifestations[dataset_id]
                nonmanifest[dataset_id] = Dataset(dataset_id=dataset_id)

        state = (
            NodeState(
                hostname=self.hostname,
                manifestations=manifestations,
                paths=paths,
            ),
        )

        if nonmanifest:
            state += (NonManifestDatasets(datasets=nonmanifest),)
        return succeed(state)

    def _mountpath_for_manifestation(self, manifestation):
        """
        Calculate a ``Manifestation`` mount point.

        :param Manifestation manifestation: The manifestation of a dataset that
            will be mounted.
        :returns: A ``FilePath`` of the mount point.
        """
        return self.mountroot.child(
            manifestation.dataset.dataset_id.encode("ascii")
        )

    def calculate_changes(self, configuration, cluster_state):
        # Eventually use the Datasets to avoid creating things that exist
        # already (https://clusterhq.atlassian.net/browse/FLOC-1575) and to
        # avoid deleting things that don't exist.
        this_node_config = configuration.get_node(self.hostname)
        configured_manifestations = this_node_config.manifestations

        configured_dataset_ids = set(
            manifestation.dataset.dataset_id
            for manifestation in configured_manifestations.values()
            # Don't create deleted datasets
            if not manifestation.dataset.deleted
        )

        local_state = cluster_state.get_node(self.hostname)
        local_dataset_ids = set(local_state.manifestations.keys())

        manifestations_to_create = set(
            configured_manifestations[dataset_id]
            for dataset_id
            in configured_dataset_ids.difference(local_dataset_ids)
        )

        # TODO prevent the configuration of unsized datasets on blockdevice
        # backends; cannot create block devices of unspecified size. FLOC-1579
        creates = list(
            CreateBlockDeviceDataset(
                dataset=manifestation.dataset,
                mountpoint=self._mountpath_for_manifestation(manifestation)
            )
            for manifestation
            in manifestations_to_create
        )

        deletes = self._calculate_deletes(configured_manifestations)

        return InParallel(changes=creates + deletes)

    def _calculate_deletes(self, configured_manifestations):
        """
        :param dict configured_manifestations: The manifestations configured
            for this node (like ``Node.manifestations``).

        :return: A ``list`` of ``DestroyBlockDeviceDataset`` instances for each
            volume that may need to be destroyed based on the given
            configuration.  A ``DestroyBlockDeviceDataset`` is returned
            even for volumes that don't exist (this is verify inefficient
            but it can be fixed later when extant volumes are included in
            cluster state - see FLOC-1616).
        """
        delete_dataset_ids = set(
            manifestation.dataset.dataset_id
            for manifestation in configured_manifestations.values()
            if manifestation.dataset.deleted
        )

        return [
            DestroyBlockDeviceDataset(dataset_id=UUID(dataset_id))
            for dataset_id
            in delete_dataset_ids
        ]
