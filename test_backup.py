import os
import time
import json
import tempfile
import unittest
import zipfile
from backup import (
    load_config,
    create_zip_archive,
    cleanup_old_backups,
    start_backup_process,
    is_parent_alive,
)


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.base_path = self.test_dir.name
        self.wheels_dir = os.path.join(self.base_path, 'wheels')
        self.output_dir = os.path.join(self.base_path, 'backups')
        self.config_path = os.path.join(self.base_path, 'config.json')
        os.makedirs(self.wheels_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_load_config_project_default(self):
        # Load the actual project config.json
        cfg = load_config()
        self.assertEqual(cfg['backup_interval_hours'], 24)
        self.assertEqual(cfg['max_backups'], 10)
        self.assertTrue(cfg['backup_run_on_startup'])
        self.assertTrue(cfg['output_dir'].endswith('backups'))
        self.assertTrue(cfg['wheels_dir'].endswith('wheels'))

    def test_load_config_missing_file_raises(self):
        non_existent = os.path.join(self.base_path, 'non_existent_config.json')
        with self.assertRaises(FileNotFoundError):
            load_config(non_existent)

    def test_load_config_custom(self):
        custom = {
            'backup_interval_hours': 12,
            'max_backups': 5,
            'output_dir': self.output_dir,
            'wheels_dir': self.wheels_dir,
            'backup_run_on_startup': False
        }
        with open(self.config_path, 'w') as f:
            json.dump(custom, f)
        cfg = load_config(self.config_path)
        self.assertEqual(cfg['backup_interval_hours'], 12)
        self.assertEqual(cfg['max_backups'], 5)
        self.assertFalse(cfg['backup_run_on_startup'])
        self.assertEqual(cfg['output_dir'], self.output_dir)

    def test_create_zip_archive(self):
        # Create dummy wheel files
        sample_file = os.path.join(self.wheels_dir, 'sample_package-1.0-py3-none-any.whl')
        with open(sample_file, 'w') as f:
            f.write('dummy content')

        archive_path = create_zip_archive(self.wheels_dir, self.output_dir)
        self.assertTrue(os.path.isfile(archive_path))
        self.assertTrue(archive_path.endswith('.zip'))

        with zipfile.ZipFile(archive_path, 'r') as zf:
            namelist = zf.namelist()
            self.assertTrue(any('sample_package-1.0-py3-none-any.whl' in name for name in namelist))

    def test_cleanup_old_backups(self):
        # Create 12 files with staggered timestamps
        created_files = []
        for i in range(12):
            f_path = os.path.join(self.output_dir, f"backup_{i:02d}.zip")
            with open(f_path, 'w') as f:
                f.write(f"backup {i}")
            # set modification time
            os.utime(f_path, (1000 + i * 10, 1000 + i * 10))
            created_files.append(f_path)

        cleanup_old_backups(self.output_dir, max_backups=10)

        remaining = sorted(os.listdir(self.output_dir))
        self.assertEqual(len(remaining), 10)
        # Oldest two (backup_00.zip, backup_01.zip) should have been deleted
        self.assertNotIn('backup_00.zip', remaining)
        self.assertNotIn('backup_01.zip', remaining)
        self.assertIn('backup_02.zip', remaining)
        self.assertIn('backup_11.zip', remaining)

    def test_start_backup_process(self):
        proc, stop_event = start_backup_process()
        self.assertTrue(proc.is_alive())
        time.sleep(0.5)
        stop_event.set()
        proc.join(timeout=3)
        self.assertFalse(proc.is_alive())

    def test_parent_alive_detection(self):
        self.assertTrue(is_parent_alive(os.getpid()))
        self.assertFalse(is_parent_alive(999999))


if __name__ == '__main__':
    unittest.main()
