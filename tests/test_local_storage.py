import pytest

from memory_system.storage.local_storage import LocalStorage


def test_user_dir_rejects_path_traversal(tmp_path):
    storage = LocalStorage(str(tmp_path))

    with pytest.raises(ValueError):
        storage._user_dir("../escape")
