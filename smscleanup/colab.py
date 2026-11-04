"""Colab helpers: turn a Google Drive folder link into a mounted path.

Only ``resolve_folder_link`` touches Colab / the Drive API; the rest is plain
logic so it can be unit-tested with a fake service object.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

MOUNT = "/content/drive"
_ID_PATTERNS = (
    re.compile(r"/folders/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"^([A-Za-z0-9_-]{10,})$"),
)


def folder_id_from_link(link: str) -> str:
    link = link.strip()
    for pat in _ID_PATTERNS:
        m = pat.search(link)
        if m:
            return m.group(1)
    raise ValueError("could not find a folder id in %r" % link)


def _get(svc, file_id: str, fields: str) -> dict:
    return svc.files().get(fileId=file_id, fields=fields, supportsAllDrives=True).execute()


def drive_path_for_folder(svc, folder_id: str, mount: str = MOUNT) -> Tuple[Optional[str], dict]:
    """Return (mounted path or None, folder info).

    Path is None when the folder is not reachable from My Drive or a shared
    drive (typically: it lives in someone else's My Drive and was only shared).
    """
    info = _get(svc, folder_id, "id,name,parents,ownedByMe,owners(emailAddress),driveId,mimeType")
    names: List[str] = [info["name"]]
    cur = info
    top = None
    while cur.get("parents"):
        try:
            cur = _get(svc, cur["parents"][0], "id,name,parents")
        except Exception:  # parent not visible to us -> shared from someone else
            return None, info
        if not cur.get("parents"):
            top = cur
            break
        names.append(cur["name"])
    names.reverse()
    if info.get("driveId"):
        drive_name = svc.drives().get(driveId=info["driveId"], fields="name").execute()["name"]
        # walk stopped at the shared drive root; it is not part of `names`
        return os.path.join(mount, "Shareddrives", drive_name, *names), info
    if top is None and not info.get("parents"):
        return None, info  # folder itself is a root we can't place
    if info.get("ownedByMe") or top is not None:
        return os.path.join(mount, "MyDrive", *names), info
    return None, info


def resolve_folder_link(link: str, mount: str = MOUNT) -> str:
    """Colab only: authenticate, resolve ``link`` to a path under the mount."""
    from google.colab import auth  # type: ignore
    from googleapiclient.discovery import build  # type: ignore

    auth.authenticate_user()
    svc = build("drive", "v3", cache_discovery=False)
    me = svc.about().get(fields="user(emailAddress)").execute()["user"]["emailAddress"]
    folder_id = folder_id_from_link(link)
    path, info = drive_path_for_folder(svc, folder_id, mount)
    owners = ", ".join(o["emailAddress"] for o in info.get("owners", [])) or "a shared drive"
    print('Folder "%s" is owned by %s; Drive API signed in as %s' % (info["name"], owners, me))

    if info.get("owners") and not info.get("ownedByMe"):
        raise SystemExit(
            "\nSTOP: %s does not own this folder.\n"
            "Only the owner can free the storage these backups use, so this notebook must be run\n"
            "by the owner. Open Colab signed in as %s, then in the mount cell run\n"
            "    drive.mount('/content/drive', force_remount=True)\n"
            "and pick %s in the popup (do the same when this cell asks for access)." % (me, owners, owners)
        )
    if path is None:
        raise SystemExit("Could not place this folder under My Drive or a shared drive.")
    if not os.path.isdir(path):
        raise SystemExit(
            "\nThe folder should be at %s but the mounted Drive does not have it.\n"
            "Drive is probably mounted as a different account than %s. Re-run the mount cell with\n"
            "    drive.mount('/content/drive', force_remount=True)\n"
            "and choose %s in the popup." % (path, me, me)
        )
    print("Using", path)
    return path
