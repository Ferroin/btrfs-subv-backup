# btrfs-subv-backup v0.1b
btrfs-subv-backup is a tool for recording the layout of subvolumes on
a mounted BTRFS filesystem in a way that can be stored in a regular
file-based backup (for example, using tar).  It originated out of a lack
of such existing tools, and is intended to be as portable as reasonably
possible.  As a result, it depends on nothing beyond a working
installation of Python version 3.4 or higher.

btrfs-subv-backup is licensed under a BSD-style license, check the
LICENSE file or the docstring for more information.

### Usage
Usage is extremely simple.  To generate a backup of a given mount
point, run:

`btrfs-subv-backup.py /path`

This will create a file called `.btrfs-subv-backup.json` in the root of
the mount point, make sure that gets included in any backups you run of
the mount point.

To restore the subvolumes in a filesystem after you've extracted a backup
of the mount point, run:

`btrfs-subv-backup.py --restore /path`

This will recreate the subvolume structure.

If you need to manually recreate the subvolumes, you can find a list
of them in the aforementioned JSON file under the 'subvolumes' key (the
other keys store info about the filesystem itself to make it easier to
figure out what it was).

### Limitations and Known Issues
* We don't store information about reflinks.  THis means in particular
that snapshot relationships __ARE NOT__ saved.  There is currently no
way to store this data reliably short of a block-level backup, which
has it's own special issues.
* Subvolumes with spaces in their name are not supported.
* There is currently no indication of progress.
* The restoration process may take a long time, and may use a very large
amount of disk space.  Ideally this should be fixed, but I'm not sure
of any way to do so while keeping things on-line.
* The current restoration process is all-or-nothing, things are not
correctly handled if some of the subvolumes have already been restored
(although btrfs-subv-backup will clean up the temporary subvolumes it
createz during the restore if the restore fails).  This should be pretty
easy to fix, I just haven't gotten around to it yet.
* btrfs-subv-backup won't cross actual mount points, which means it
won't recurse into explicitly mounted subvolumes.  This makes usage a
bit more complicated on some distributions (such as SLES and OpenSUSE),
but greatly simplifies the code.
