# btrfs-subv-backup v0.3b
btrfs-subv-backup is a tool for recording the layout of subvolumes on
a mounted BTRFS filesystem in a way that can be stored in a regular
file-based backup (for example, using tar).  It originated out of
a lack of such existing tools, and is intended to be as portable as
reasonably possible.

btrfs-subv-backup is licensed under a 3-clause BSD license, check the
LICENSE file or the docstring for more information.

### Dependencies

btrfs-subv-backup requires a working installation of Python 3.X  I've
only tested it on 3.4 and 3.6, but I expect it should work fine on most
other versions of Python 3.

It also depends on:
* BTRFS support in the Linux kernel: It should work with any version
  that supports BTRFS, though it hasn't been thoroughly tested on anything
  before version 4.10.  If you have a particularly old kernel, reflinks
  may not be possible, and btrfs-subv-backup will fall back to using a
  direct copy method for converting directories in-place.
* btrfs-progs: Only the subvolume list, create, and delete commands
  are used, and the only command that is likely to cause issues is the
  list command.  I've tested it on versions as far back as 4.10.2,
  but I expect it should work with earlier versions as well, just like with
  the kernel.
* util-linux: Specifically the `blkid` command.  The options that are
  used have been around for longer than BTRFS has, so it's very unlikely
  that you will see any issues with whatever version you have installed.
  Tested on 2.28.2 and 2.31.  Not having a working blkid will not prevent
  the script from operating correctly, it just won't store the filesystem
  label and UUID with the subvolume information.

There is also an optional dependency on the Python 'reflink' module
(https://pypi.python.org/pypi/reflink).  This will make the process of
subvolume restoration when data is present significantly more efficient
(both in terms of time and disk usage).  If this module is not present,
btrfs-subv-backup will fall back to a direct copy method of restoration,
which is not very efficient.

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

This will recreate the subvolume structure.  It can be run either on an
empty directory with the JSON file in it's root (in which case it will
also create intermediary directories), or on an already existing directory
tree, in which case it will copy the existing data into the subvolumes.

If you want some progress messages, add `--verbose` to the command line.

If you need to manually recreate the subvolumes, you can find a list
of them in the aforementioned JSON file under the 'subvolumes' key (the
other keys store info about the filesystem itself to make it easier to
figure out what it was).

You can also use btrfs-subv-backup to convert an existing directory to
a subvolume in-place.  To do so, pass the `--convert` option, followed
by the path to convert.

### Limitations and Known Issues
* We __DO NOT__ store information about reflinks.  This means in particular
that snapshot relationships __ARE NOT__ saved.  There is currently no
way to store this data reliably short of a block-level backup, which
has it's own special issues.
* Subvolumes with spaces in their name are not supported.
* When restoring subvolumes in a pre-existing directory tree, the
restoration process does not reliably copy POSIX ACL's or security
extended attributes (such as SELinux context).
* btrfs-subv-backup won't cross actual mount points, which means it
won't recurse into explicitly mounted subvolumes.  This makes usage a
bit more complicated on some distributions (such as SLES and OpenSUSE),
but greatly simplifies the code.
