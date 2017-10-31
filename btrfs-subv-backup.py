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
   All rights reserved.

   Redistribution and use in source and binary forms, with or without
   modification, are permitted provided that the following conditions
   are met:

   * Redistributions of source code must retain the above copyright
     notice, this list of conditions and the following disclaimer.

   * Redistributions in binary form must reproduce the above copyright
     notice, this list of conditions and the following disclaimer in the
     documentation and/or other materials provided with the distribution.

   * Neither the name of the copyright holder nor the names of its
     contributors may be used to endorse or promote products derived
     from this software without specific prior written permission.

   THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
   "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
   LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
   A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
   HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
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
import subprocess
import sys

try:
    import reflink
except ImportError:
    pass

_VERSION = '0.3b'
_DESCRIPTION = '''
btrfs-subv-backup is a tool for backing up the BTRFS subvolume layout
below a given mount point.

It creates a file called .btrfs-subv-backup.json under the given mount
point, which contains enough data to recreate the subvolume layout,
as well as some secondary info to help humans looking at it figure out
what filesystem it was generated from.

btrfs-subv-backup explicitly DOES NOT store information about snapshot
relationshipts or reflinks.  It will not restore the nature of snapshots.
It also will not cross mount boundaries, which may somewhat complicate
things for people using certain distributions that explicitly mount all
the subvolumes of the root volume.

btrfs-subv-backup is also capable of restoring the state it saves.  To do
so, make sure the .btrfs-subv-backup.json file is in the root of the
mount point, and then call btrfs-subv-backup on the mount point with the
'--restore' option.  This will recreate the subvolumes in-place, and may
disrupt timestamps when doing so.  Restoration can be done with reflinks,
or via a direct copy.  The reflink method will be used by default if
the required library is present, otherwise a direct copy will be done.
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

def get_fs_info(path, verbose=False):
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
    if verbose:
        print('Fetching filesystem information for ' + path)
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
    try:
        ret['uuid'] = subprocess.check_output(['blkid', '-o', 'value', '-s', 'UUID', ret['device']]).decode().rstrip()
        ret['label'] = subprocess.check_output(['blkid', '-o', 'value', '-s', 'LABEL', ret['device']]).decode().rstrip()
    except subprocess.CalledProcessError:
        pass
    return ret

def get_subvol_list(fsinfo, verbose=False):
    '''Parse the subvolume tree in the mountpoint given by fsinfo.

      This returns fsinfo, with the subvolume tree added to it under the
      'subvolumes' key.  Each subvolume except the root is represented
      by name in a list under the 'subvolumes' key.  If there are no
      subvolumes other than the root, the list will be empty.

      This is horribly slow, and could be made much more efficient.'''
    ret = fsinfo
    ret['subvolumes'] = list()
    if verbose:
        print('Generating subvolume list for ' + fsinfo['path'])
    for root, dirs, files in os.walk(fsinfo['path']):
        exclude = list()
        for item in dirs:
            fullpath = os.path.join(root, item)
            if _ismount(fullpath):
                exclude.append(item)
                continue
            elif os.stat(fullpath, follow_symlinks=False).st_ino == 256:
                if verbose:
                    print('Found subvolume at ' + os.path.join(root[len(fsinfo['path']):].lstrip('/'), item))
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

def copy_ownership(src, dest):
    '''Copy the file owner and group from src to dest.'''
    status = os.stat(src, follow_symlinks=True)
    os.chown(dest, status.st_uid, status.st_gid)

def copytree(src, dest, method, verbose=False):
    '''Custom version of shutil.copytree().

      This exists so that we can copy ownership properly, and so we can
      use reflinks if they're availble.

      'method' should be one of 'reflink', or 'copy'.'''
    srcpath = os.path.abspath(src)
    destpath = os.path.abspath(dest)
    oldcwd = os.getcwd()
    os.chdir(srcpath)
    for root, dirs, files in os.walk('.'):
        for item in dirs:
            srcdir = os.path.join(srcpath, root, item)
            destdir = os.path.join(destpath, root, item)
            os.makedirs(destdir)
            if os.geteuid() == 0:
                copy_ownership(srcdir, destdir)
            shutil.copystat(srcdir, destdir)
        for item in files:
            srcfile = os.path.join(srcpath, root, item)
            destfile = os.path.join(destpath, root, item)
            if method == 'reflinks':
                try:
                    reflink.reflink(srcfile, destfile)
                except reflink.ReflinkImpossibleError:
                    if verbose:
                        print('ERROR reflinking file, copying instead.')
                    shutil.copyfile(srcfile, destfile)
            else:
                shutil.copyfile(srcfile, destfile)
            if os.geteuid() == 0:
                copy_ownership(srcdir, destdir)
            shutil.copystat(srcdir, destdir)
    os.chdir(oldcwd)

def convert_dir_to_subv(dest, method, verbose=False):
    '''Convert a directory to a subvolume, in-place.

       This takes one argument, the destination path to convert.  It will
       raise an error if the path is not a directory, and will reduce
       to doing nothing if the destination is already a subvolume.

       This does a functionally in-place restore using a double rename.
       As a result of having to copy eveyrthing already at the given
       location, it can take a very long time.  Hopefully BTRFS will
       some day add the ability to actually convert a directory to a
       subvolume in-place.

       Note also that Python has no cross-rename support, so it is
       possible for this function to fail hard.'''
    path, subvol = os.path.split(dest)
    if not os.path.isdir(dest):
        raise OSError('Subvolume destination exists and is not a directory:' + subvol)
    elif os.stat(dest, follow_symlinks=False).st_ino == 256:
        return True
    temppath = os.path.abspath(gen_rand_subvolpath(path, subvol))
    copypath = os.path.abspath(os.path.join(path, '.btrfs-subv-backup.tmp'))
    with open(copypath, 'w+') as tmp:
        tmp.close()
    try:
        subprocess.check_output(['btrfs', 'subvolume', 'create', temppath])
    except subprocess.CalledProcessError:
        raise OSError('Unable to create temporary subvolume:' + subvol)
    try:
        oldpath = os.path.abspath(os.path.join(path, '.btrfs-subv-backup.old'))
        shutil.copystat(dest, copypath, follow_symlinks=True)
        copytree(dest, temppath, method, verbose)
        os.rename(dest, oldpath)
        os.rename(temppath, dest)
        shutil.copystat(copypath, dest, follow_symlinks=True)
        if os.geteuid() == 0:
            copy_ownership(oldpath, dest)
        shutil.rmtree(oldpath)
    finally:
        try:
            subprocess.check_output(['btrfs', 'subvolume', 'delete', temppath])
            os.unlink(copypath)
        except subprocess.CalledProcessError:
            pass
    return True

def restore_subvol(path, subvol, method, verbose=False):
    '''Restore a subvolume under path.

       If the path exists, it is converted to a subvolume using
       convert_dir_to_subv(), otherwise we just create the subvolume
       (and 'ny intermediary directories).'''
    destpath = os.path.abspath(os.path.join(path, subvol))
    os.makedirs(os.path.split(destpath)[0], exist_ok=True)
    if _ismount(destpath):
        raise OSError('Subvolume destination is a mount point, unable to continue:' + destpath)
    if os.path.isdir(destpath):
        if verbose:
            print('Converting directory to subvolume at ' + os.path.join(path, subvol))
        convert_dir_to_subv(destpath, method, verbose)
    elif not os.path.exists(destpath):
        if verbose:
            print('Creating subvolume at ' + os.path.join(path, subvol))
        try:
            subprocess.check_output(['btrfs', 'subvolume', 'create', destpath])
        except subprocess.CalledProcessError:
            raise OSError("Unable to create subvolume:" + subvol)
    else:
        raise OSError('Subvolume destination exists and is not a directory:' + subvol)

def parse_args():
    '''Parse the command-line arguments.'''
    parser = argparse.ArgumentParser(description=_DESCRIPTION)
    parser.add_argument('--version', '-V', action='version', version=_VERSION)
    parser.add_argument('--save', '-s', action='store_const', dest='mode', const='save', default='save',
                        help='Save the state of the given mount point (this is the default).')
    parser.add_argument('--restore', '-r', action='store_const', dest='mode', const='restore', default='save',
                        help='Restore the state of the given mount point.')
    parser.add_argument('--convert', '-c', action='store_const', dest='mode', const='convert', default='save',
                        help='Convert the path to a subvolume in-place.  Does not work on mount-points.')
    parser.add_argument('path', help='The path to the mount point to operate on.')
    parser.add_argument('--verbose', '-v', action='store_const', dest='verbose', const=True, default=False,
                        help='Print out status messages as things happen.')
    parser.add_argument('--method', '-m', nargs='1', dest='method', default='reflink',
                        help='Select a particular restore method.  Available options are reflink and copy.')
    args = parser.parse_args()
    if args.mode == 'restore':
        if args.method == 'reflink':
            if reflink in dir():
                print('Using reflink restoration method')
            else:
                print('Unable to use reflink restore method due to missing libraries, falling back to copy method')
                args.method = 'copy'
        elif args.method == 'copy':
            print('Using copy restore method')
        else:
            raise Exception('Unknown restore method ' + args.method)
    return args

def main():
    '''Main program logic.'''
    args = parse_args()
    if args.mode == 'save':
        fsinfo = get_fs_info(args.path, verbose=args.verbose)
        fsinfo = get_subvol_list(fsinfo, verbose=args.verbose)
        if args.verbose:
            print('Writing subvolume information')
        with open(os.path.join(args.path, '.btrfs-subv-backup.json'), 'w+') as jfile:
            return json.dump(fsinfo, jfile, sort_keys=True, indent=4)
    elif args.mode == 'restore':
        fsinfo = get_fs_info(args.path, verbose=args.verbose)
        if verbose:
            print('Loading subvolume information')
        with open(os.path.join(args.path, '.btrfs-subv-backup.json'), 'r') as jfile:
            state = json.load(jfile)
        state['subvolumes'].sort()
        for item in state['subvolumes']:
            restore_subvol(args.path, item, method=args.method, verbose=args.verbose)
    elif args.mode == 'convert':
        if verbose:
            print('Converting ' + path + ' to a subvolume in-place.')
        path, subvol = os.path.split(args.path)
        restore_subvol(path, subvol, method=args.method, verbose=args.verbose)
    else:
        raise Exception('Unhandled operating mode: ' + args.mode)

if __name__ == '__main__':
    sys.exit(main())
