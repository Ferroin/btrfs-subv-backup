#!/usr/bin/env python3
# vi:set sw=4 sts:
'''btrfs-subv-backup: BTRFS subvolume layout backup tool.

   btrfs-subv-backup is a tool for recording the layout of subvolumes on a mounted
   BTRFS filesystem in a way that can be stored in a regular file-based
   backup (for example, using tar).  Note that it _only_ stores the
   subvolume layout, _NOT_ reflinks, so it won't preserve snapshot
   relationships.  It also does not track subvolumes beyond the mount
   point it's passed.

   Note that we do not handle subvolumes with spaces in the name, or
   explicit subvolume mounts.

   Check btrfs-subv-backupup.py --help for usage information.

   Copyright (c) 2017, Austin S. Hemmelgarn
   All rights reserved

   Redistribution and use in source and binary forms, with or without
   modification, are permitted provided that the following conditions
   are met:

   1. Redistributions of source code must retain the above copyright
      notice,  this list of conditions and the following disclaimer.
   2. Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
   3. Neither the name of btrfs-subv-backup  nor the names of its
      contributors may be used to endorse or promote products derived
      from this software without specific prior written permission.

   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
   "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
   LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
   A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
   OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
   SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
   LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
   DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
   THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
   (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
   OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
'''

import argparse
import base64
import json
import os
import random
import shutil
import stat
import subprocess
import sys
from queue import Queue as queue

_VERSION = '0.1b'
_DESCRIPTION = '''
btrfs-subv-backup is a tool for backing up the BTRFS subvolume layout below a given mount point.

It creates a file called .btrfs-subv-backup.json under the given mount point, which
contains enough data to recreate the subvolume layout, as well as some
secondary info to help humans looking at it figure out what filesystem
it was generated from.

btrfs-subv-backup explicitly DOES NOT store information about snapshot relationshipts
or reflinks.  It will not restore the nature of snapshots.  It also
will not cross mount boundaries, which may somewhat complicate things
for people using certain distributions that explicitly mount all the
subvolumes of the root volume.

btrfs-subv-backup is also capable of restoring the state it saves.  To do so,
make sure the .btrfs-subv-backup.json file is in the root of the mount point,
and then call btrfs-subv-backup on the mount point with the '--restore' option.
This will recreate the subvolumes in-place, and may disrupt timestamps
when doing so.
'''

def _ismount(path):
    '''Determine if something is a mountpoint.

       This exists to work around the fact that BTRFS subvolumes return
       true for os.path.ismount(), regardless of whether they've been
       manually mounted or not.  It checks directly in /proc/mounts to
       determine if the path was manually mounted or not.'''
    if os.path.ismount(path):
        mntpath = os.path.abspath(path)
        with open('/proc/mounts', 'r') as mntinfo:
            for line in mntinfo:
                if os.path.abspath(line.split()[1]) == mntpath:
                    return True
    return False

def get_fs_info(path):
    '''Retrieve the filesystem info for the given mountpoint.

       This parses through /proc/mounts, matching on the path given,
       and also retrieves some info from other sources, and returns a
       dictionary with the follwoing keys:

       - path: The mount path
       - device: The device the filesystem is mounted from.
       - uuid: The filesystem UUID.
       - label: The filesystem label.
       - subvolume: The name of the mounted subvolume.
       - subvolid: The ID of the mounted subvolume.

       This could probably be made more efficient.'''
    ret = {
        'path': os.path.abspath(path),
        'device': None,
        'uuid': None,
        'label': None,
        'subvolume': None,
        'subvolid': None
    }
    mntent = False
    with open('/proc/mounts', 'r') as mounts:
        for line in mounts:
            entry = line.split()
            if entry[2] == 'btrfs' and entry[1] == ret['path']:
                mntent = entry
                break
        if not mntent:
            raise ValueError(ret['path'] + ' is not a mountpoint, or is not a BTRFS filesystem.')
    ret['device'] = mntent[0]
    mntopts = mntent[3].split(',')
    for item in mntopts:
        if item.startswith('subvolid='):
            ret['subvolid'] = int(item.partition('=')[2])
        elif item.startswith('subvol='):
            ret['subvolume'] = item.partition('=')[2]
    if not ret['subvolid']:
        raise ValueError('Unable to determine mounted subvolume ID for ' + ret['path'])
    ret['uuid'] = subprocess.check_output(['blkid', '-o', 'value', '-s', 'UUID', ret['device']]).decode().rstrip()
    ret['label'] = subprocess.check_output(['blkid', '-o', 'value', '-s', 'LABEL', ret['device']]).decode().rstrip()
    return ret

def get_subvol_list(fsinfo):
    '''Parse the subvolume tree in the mountpoint given by fsinfo.

      This returns fsinfo, with the subvolume tree added to it under the
      'subvolumes' key.  Each subvolume except the root is represented
      by name in a list under the 'subvolumes' key.  If there are no
      subvolumes other than the root, the list will be empty.

      This is horribly slow, and could be made much more efficient.'''
    ret = fsinfo
    ret['subvolumes'] = list()
    for root, dirs, files in os.walk(fsinfo['path']):
        exclude = list()
        for item in dirs:
            fullpath = os.path.join(root, item)
            if _ismount(fullpath):
                exclude.append(item)
                continue
            elif os.stat(fullpath, follow_symlinks=False).st_ino == 256:
                ret['subvolumes'].append(os.path.join(root[len(fsinfo['path']):].lstrip('/'), item))
        dirs[:] = [d for d in dirs if d not in exclude]
    ret['subvolumes'].sort()
    return ret

def gen_rand_subvolpath(path, subvol):
    '''Generate a random subvolume path based on the given path and subvolume.

       This is used to create a viable temporary subvolume name for
       temporary usage while copying data.'''
    seed = base64.urlsafe_b64encode(random.getrandbits(64).to_bytes(16, byteorder='big', signed=True)).decode()
    dest = os.path.split(os.path.join(path, subvol))
    return os.path.join(dest[0], '.' + dest[1] + '.' + seed)

def restore_subvol(path, subvol):
    '''Restore a subvolume under path.

       This does a functionally in-place restore using a double rename.
       As a result of having to copy eveyrthing already at the given
       location, it can take a very long time.  Hopefully BTRFS will
       some day add the ability to actually convert a directory to a
       subvolume in-place.

       Note also that Python has no cross-rename support, so it is
       possible for this function to fail hard.'''
    temppath = gen_rand_subvolpath(path, subvol)
    destpath = os.path.join(path, subvol)
    os.makedirs(os.path.split(temppath)[0], exist_ok=True)
    try:
        subprocess.check_output(['btrfs', 'subvolume', 'create', temppath])
    except subprocess.CalledProcessError:
        raise OSError('Unable to create temporary subvolume:' + subvol)
    try:
        if os.path.isdir(destpath):
            shutil.copytree(destpath, temppath, symlinks=True)
            os.rename(destpath, os.path.join(path, '.btrfs-subv-backup.old'))
            os.rename(temppath, destpath)
            shutil.rmtree(os.path.join(path, '.btrfs-subv-backup.old'))
        elif not os.path.exists(destpath):
            os.rename(temppath, destpath)
        else:
            raise OSError('Subvolume destination exists and is not a directory:' + subvol)
    finally:
        try:
            subprocess.check_output(['btrfs', 'subvolume', 'delete', temppath])
        except:
            pass

def parse_args():
    '''Parse the command-line arguments.'''
    parser = argparse.ArgumentParser(description=_DESCRIPTION)
    parser.add_argument('--version', '-V', action='version', version=_VERSION)
    parser.add_argument('--save', '-s', action='store_const', dest='mode', const='save', default='save',
                        help='Save the state of the given mount point (this is the default).')
    parser.add_argument('--restore', '-r', action='store_const', dest='mode', const='restore', default='save',
                        help='Restore the state of the given mount point.')
    parser.add_argument('path', help='The path to the mount point to operate on.')
    args = parser.parse_args()
    return args

def main():
    '''Main program logic.'''
    args = parse_args()
    if args.mode == 'save':
        fsinfo = get_fs_info(args.path)
        fsinfo = get_subvol_list(fsinfo)
        with open(os.path.join(args.path, '.btrfs-subv-backup.json'), 'w+') as jfile:
            return json.dump(fsinfo, jfile, sort_keys=True, indent=4)
    elif args.mode == 'restore':
        fsinfo = get_fs_info(args.path)
        with open(os.path.join(args.path, '.btrfs-subv-backup.json'), 'r') as jfile:
            state = json.load(jfile)
        state['subvolumes'].sort()
        for item in state['subvolumes']:
            restore_subvol(args.path, item)
    else:
        raise Exception('Unhandled operating mode: ' + args.mode)

if __name__ == '__main__':
    sys.exit(main())
