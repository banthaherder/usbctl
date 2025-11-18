#!/usr/bin/env python3
import argparse
import plistlib
import subprocess
from pathlib import Path
import sys

# global debug flag
DEBUG = False

# base directory for virtual disks
BASE_DIR = Path.home() / ".usbctl"
BASE_DIR.mkdir(exist_ok=True)

# ----- helpers -----


def debug_print(msg: str) -> None:
    if DEBUG:
        print(f"[debug] {msg}")


def run_plist(args: list) -> dict:
    """
    run a command that returns plist output
    """
    debug_print(f"exec: {' '.join(args)}")
    proc = subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return plistlib.loads(proc.stdout)


def run_text(args: list) -> str:
    """
    run a command that returns plain text
    """
    debug_print(f"exec: {' '.join(args)}")
    proc = subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.stdout


def get_hdiutil_info() -> dict:
    """
    get current hdiutil image info as plist
    """
    try:
        return run_plist(["hdiutil", "info", "-plist"])
    except subprocess.CalledProcessError:
        return {"images": []}


def find_image_entry_for_path(path: Path):
    """
    locate the hdiutil image entry for a given raw file path
    """
    info = get_hdiutil_info()
    for img in info.get("images", []):
        img_path = img.get("image-path")
        if img_path and Path(img_path) == path:
            return img
    return None


def get_base_device_from_image(img_entry: dict) -> str | None:
    """
    find the base device (/dev/diskx) for an image entry
    """
    # prefer entities without mount-point
    for ent in img_entry.get("system-entities", []):
        dev = ent.get("dev-entry")
        if dev and "mount-point" not in ent:
            return dev
    # fallback: first dev-entry
    for ent in img_entry.get("system-entities", []):
        dev = ent.get("dev-entry")
        if dev:
            return dev
    return None


def get_mountpoints_from_image(img_entry: dict) -> list[str]:
    """
    collect all mount points for an image entry
    """
    mps: list[str] = []
    for ent in img_entry.get("system-entities", []):
        mp = ent.get("mount-point")
        if mp:
            mps.append(mp)
    return mps


def get_mountpoints_from_disk(device: str) -> list[str]:
    """
    inspect diskutil list -plist to find mountpoints under a base disk
    """
    try:
        plist = run_plist(["diskutil", "list", "-plist", device])
    except subprocess.CalledProcessError:
        return []

    mount_points: list[str] = []

    for disk_entry in plist.get("AllDisksAndPartitions", []):
        # ensure this is our base device
        if disk_entry.get("DeviceIdentifier") not in (device.replace("/dev/", ""),):
            continue

        for part in disk_entry.get("Partitions", []) or []:
            mp = part.get("MountPoint")
            if mp:
                mount_points.append(mp)

    return mount_points


# ----- core operations -----


def validate_disk_name(name: str) -> bool:
    """
    validate disk name meets filesystem requirements
    volume labels for FAT/ExFAT are limited to 11 characters
    """
    if len(name) > 11:
        print(f"error: disk name '{name}' is too long (max 11 characters)")
        print(f"  current length: {len(name)}")
        return False
    if not name:
        print("error: disk name cannot be empty")
        return False
    # check for invalid characters
    invalid_chars = ["/", "\\", ":", "*", "?", '"', "<", ">", "|"]
    for char in invalid_chars:
        if char in name:
            print(f"error: disk name contains invalid character '{char}'")
            return False
    return True


def create_disk(name: str, size_gb: float, fs: str = "ExFAT") -> None:
    """
    create a new virtual disk file, attach it as raw, and format it
    """
    if not validate_disk_name(name):
        return

    disk_path = BASE_DIR / f"{name}.raw"
    if disk_path.exists():
        print(f"error: disk '{name}' already exists at {disk_path}")
        return

    size_bytes = int(size_gb * (1024**3))
    print(f"creating raw file {disk_path} ({size_gb} gb)...")
    try:
        with open(disk_path, "wb") as f:
            f.truncate(size_bytes)
    except OSError as e:
        print(f"failed to create disk file: {e}")
        return

    print("attaching raw disk via hdiutil...")
    try:
        attach_plist = run_plist(
            [
                "hdiutil",
                "attach",
                "-imagekey",
                "diskimage-class=CRawDiskImage",
                "-nomount",
                "-plist",
                str(disk_path),
            ]
        )
    except subprocess.CalledProcessError as e:
        print("hdiutil attach failed")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr
        if err:
            print(err.strip())
        return

    device = None
    for ent in attach_plist.get("system-entities", []):
        dev = ent.get("dev-entry")
        if dev:
            device = dev
            break

    if not device:
        print("could not determine device for attached raw disk")
        return

    label = name.upper()
    print(f"formatting {device} as {fs} with label {label}...")
    try:
        # eraseDisk does not support -plist; use plain text
        run_text(["diskutil", "eraseDisk", fs, label, "GPT", device])
    except subprocess.CalledProcessError as e:
        print("diskutil eraseDisk failed")
        err = e.stderr if isinstance(e.stderr, str) else e.stderr.decode()
        if err:
            print(err.strip())
        # best-effort cleanup hint
        print(f"you may need to run: hdiutil detach {device} && rm '{disk_path}'")
        return

    # try to discover mountpoints via diskutil list -plist
    mount_points = get_mountpoints_from_disk(device)
    if mount_points:
        print(f"disk created and mounted at: {', '.join(mount_points)}")
    else:
        # fallback: maybe auto-mounted but not visible from list; user can inspect manually
        print("disk created, but mount point could not be determined")
        print("you can inspect with: diskutil list && ls /Volumes")


def list_disks() -> None:
    """
    list managed disks and their status
    """
    print(f"managed disks in {BASE_DIR}:")
    disks = sorted(BASE_DIR.glob("*.raw"))
    if not disks:
        print("  (none)")
        return

    for f in disks:
        name = f.stem
        img = find_image_entry_for_path(f)
        if img:
            dev = get_base_device_from_image(img)
            mps = get_mountpoints_from_image(img)
            status = "attached"
            if mps:
                status = f"mounted at {', '.join(mps)}"
            print(f"  - {name}: {status} (file={f}, device={dev})")
        else:
            print(f"  - {name}: detached (file={f})")


def mount_disk(name: str) -> None:
    """
    mount an existing disk (attach and auto-mount)
    """
    disk_path = BASE_DIR / f"{name}.raw"
    if not disk_path.exists():
        print(f"error: disk '{name}' does not exist")
        return

    img = find_image_entry_for_path(disk_path)
    if img:
        mps = get_mountpoints_from_image(img)
        dev = get_base_device_from_image(img)
        if mps:
            print(f"disk '{name}' already mounted at: {', '.join(mps)}")
        else:
            print(f"disk '{name}' is attached as {dev} but has no mountpoint")
        return

    print(f"attaching and mounting {disk_path}...")
    try:
        attach_plist = run_plist(
            [
                "hdiutil",
                "attach",
                "-imagekey",
                "diskimage-class=CRawDiskImage",
                "-plist",
                str(disk_path),
            ]
        )
    except subprocess.CalledProcessError as e:
        print("hdiutil attach failed")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr
        if err:
            print(err.strip())
        return

    mps: list[str] = []
    for ent in attach_plist.get("system-entities", []):
        mp = ent.get("mount-point")
        if mp:
            mps.append(mp)

    if mps:
        print(f"mounted at: {', '.join(mps)}")
    else:
        print("attached, but no mountpoint reported")
        print("you can inspect with: hdiutil info -plist && ls /Volumes")


def unmount_disk(name: str) -> None:
    """
    detach a disk by name (unmount volumes and remove device)
    """
    disk_path = BASE_DIR / f"{name}.raw"
    if not disk_path.exists():
        print(f"error: disk '{name}' does not exist")
        return

    img = find_image_entry_for_path(disk_path)
    if not img:
        print(f"disk '{name}' is not currently attached")
        return

    dev = get_base_device_from_image(img)
    if not dev:
        print("could not determine device to detach")
        return

    print(f"detaching {dev} for disk '{name}'...")
    try:
        run_text(["hdiutil", "detach", dev])
        print("detached")
    except subprocess.CalledProcessError as e:
        print("hdiutil detach failed")
        err = e.stderr if isinstance(e.stderr, str) else e.stderr.decode()
        if err:
            print(err.strip())


def delete_disk(name: str) -> None:
    """
    delete the raw disk file (requires it to be detached)
    """
    disk_path = BASE_DIR / f"{name}.raw"
    if not disk_path.exists():
        print(f"error: disk '{name}' does not exist")
        return

    img = find_image_entry_for_path(disk_path)
    if img:
        print("disk is still attached; unmount it first (usbctl unmount NAME)")
        return

    print(f"deleting {disk_path}...")
    try:
        disk_path.unlink()
        print("deleted")
    except OSError as e:
        print(f"failed to delete disk file: {e}")


def info_disk(name: str) -> None:
    """
    show detailed info for a managed disk
    """
    disk_path = BASE_DIR / f"{name}.raw"
    if not disk_path.exists():
        print(f"error: disk '{name}' does not exist")
        return

    print(f"disk '{name}':")
    print(f"  file: {disk_path}")
    try:
        size = disk_path.stat().st_size
        print(f"  size: {size} bytes")
    except OSError:
        pass

    img = find_image_entry_for_path(disk_path)
    if not img:
        print("  status: detached")
        return

    dev = get_base_device_from_image(img)
    mps = get_mountpoints_from_image(img)
    print(f"  status: attached")
    print(f"  device: {dev}")
    if mps:
        for mp in mps:
            print(f"  mount: {mp}")
    else:
        print("  mount: (none)")


# ----- cli -----


def main() -> None:
    global DEBUG

    parser = argparse.ArgumentParser(
        prog="usbctl",
        description="virtual usb disk manager for macos",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print every system command before executing it",
    )

    sub = parser.add_subparsers(dest="cmd")

    p_create = sub.add_parser("create", help="create a new disk")
    p_create.add_argument("name", help="disk name")
    p_create.add_argument("size_gb", type=float, help="size in gigabytes")
    p_create.add_argument("--fs", default="ExFAT", help="filesystem (default: ExFAT)")

    sub.add_parser("list", help="list managed disks")

    p_mount = sub.add_parser("mount", help="mount a disk")
    p_mount.add_argument("name")

    p_unmount = sub.add_parser("unmount", help="unmount / detach a disk")
    p_unmount.add_argument("name")

    p_delete = sub.add_parser("delete", help="delete a disk file")
    p_delete.add_argument("name")

    p_info = sub.add_parser("info", help="show detailed info for a disk")
    p_info.add_argument("name")

    args = parser.parse_args()

    DEBUG = args.debug

    if args.cmd == "create":
        create_disk(args.name, args.size_gb, fs=args.fs)
    elif args.cmd == "list":
        list_disks()
    elif args.cmd == "mount":
        mount_disk(args.name)
    elif args.cmd == "unmount":
        unmount_disk(args.name)
    elif args.cmd == "delete":
        delete_disk(args.name)
    elif args.cmd == "info":
        info_disk(args.name)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
