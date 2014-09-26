#!/usr/bin/python

# This script builds the base flocker-dev box.

import sys, os
from subprocess import check_call, check_output
from textwrap import dedent

if len(sys.argv) > 3:
    print "Wrong number of arguments."
    raise SystemExit(1)

if len(sys.argv) > 1:
    version = sys.argv[1]
if len(sys.argv) > 2:
    branch = sys.argv[2]

# Make it possible to install flocker-node
rpm_dist = check_output(['rpm', '-E', '%dist']).strip()
check_call(['yum', 'install', '-y', 'https://s3.amazonaws.com/archive.zfsonlinux.org/fedora/zfs-release%s.noarch.rpm' % (rpm_dist,)])
check_call(['yum', 'install', '-y', 'https://storage.googleapis.com/archive.clusterhq.com/fedora/clusterhq-release%s.noarch.rpm' % (rpm_dist,)])

if branch:
    with open('/etc/yum.repos.d/clusterhq-build.repo', 'w') as repo:
        repo.write(dedent(b"""
            [clusterhq-build]
            name=clusterhq-build
            baseurl=http://build.clusterhq.com/results/fedora/20/x86_64/%s
            gpgcheck=0
            enabled=0
            """) % (branch,))
    branch_opt = ['--enablerepo=clusterhq-build']
else:
    branch_opt = []
if version:
    package = 'flocker-node-%s' % (version,)
else:
    package = 'flocker-node'
check_call(['yum', 'install', '-y'] + branch_opt + [package])

check_call(['systemctl', 'enable', 'docker'])

# Make it easy to authenticate as root
check_call(['mkdir', '-p', '/root/.ssh'])
check_call(['cp', os.path.expanduser('~vagrant/.ssh/authorized_keys'), '/root/.ssh'])

# Create a ZFS storage pool backed by a normal filesystem file.  This
# is a bad way to configure ZFS for production use but it is
# convenient for a demo in a VM.
check_call(['mkdir', '-p', '/opt/flocker'])
check_call(['truncate', '--size', '1G', '/opt/flocker/pool-vdev'])
check_call(['zpool', 'create', 'flocker', '/opt/flocker/pool-vdev'])