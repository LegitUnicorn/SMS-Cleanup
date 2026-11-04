import pytest

from smscleanup.colab import drive_path_for_folder, folder_id_from_link

FID = "1lEYP-PU13Rdi5u7hMlRNR0tlv9mRbQ8d"


def test_folder_id_from_link():
    assert folder_id_from_link("https://drive.google.com/drive/folders/%s?usp=drive_link" % FID) == FID
    assert folder_id_from_link("https://drive.google.com/drive/u/1/folders/%s" % FID) == FID
    assert folder_id_from_link("https://drive.google.com/open?id=%s" % FID) == FID
    assert folder_id_from_link(FID) == FID
    with pytest.raises(ValueError):
        folder_id_from_link("https://drive.google.com/drive/my-drive")


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        if isinstance(self.v, Exception):
            raise self.v
        return self.v


class FakeDrive:
    def __init__(self, files, drives=None):
        self._files, self._drives = files, drives or {}

    def files(self):
        return self

    def drives(self):
        outer = self

        class D:
            def get(self, driveId, fields):
                return _Exec(outer._drives[driveId])

        return D()

    def get(self, fileId, fields, supportsAllDrives):
        return _Exec(self._files.get(fileId, LookupError("404 " + fileId)))


def test_path_in_my_drive():
    svc = FakeDrive(
        {
            FID: {"id": FID, "name": "SMS", "parents": ["p1"], "ownedByMe": True, "owners": [{"emailAddress": "me@x"}]},
            "p1": {"id": "p1", "name": "Phone Backups", "parents": ["root"]},
            "root": {"id": "root", "name": "My Drive"},
        }
    )
    path, info = drive_path_for_folder(svc, FID)
    assert path == "/content/drive/MyDrive/Phone Backups/SMS"
    assert info["ownedByMe"]


def test_folder_directly_in_my_drive_root():
    svc = FakeDrive(
        {
            FID: {"id": FID, "name": "SMSBackupRestore", "parents": ["root"], "ownedByMe": True},
            "root": {"id": "root", "name": "My Drive"},
        }
    )
    assert drive_path_for_folder(svc, FID)[0] == "/content/drive/MyDrive/SMSBackupRestore"


def test_shared_with_me_has_no_path():
    svc = FakeDrive({FID: {"id": FID, "name": "SMS", "ownedByMe": False, "owners": [{"emailAddress": "o@x"}]}})
    path, info = drive_path_for_folder(svc, FID)
    assert path is None and not info["ownedByMe"]


def test_parent_not_visible():
    svc = FakeDrive({FID: {"id": FID, "name": "SMS", "parents": ["hidden"], "ownedByMe": False}})
    assert drive_path_for_folder(svc, FID)[0] is None


def test_shared_drive():
    svc = FakeDrive(
        {
            FID: {"id": FID, "name": "SMS", "parents": ["sdroot"], "driveId": "sd"},
            "sdroot": {"id": "sdroot", "name": "Drive"},
        },
        drives={"sd": {"name": "Team"}},
    )
    assert drive_path_for_folder(svc, FID)[0] == "/content/drive/Shareddrives/Team/SMS"
