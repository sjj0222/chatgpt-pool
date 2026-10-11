"""故障注入：发布链任一步失败都必须恢复发布前的有效池。"""
import os
import unittest
from unittest import mock

from helpers import make_cfg
import pipeline
import poolstore


class TestPublishTransaction(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg("publish_tx")
        poolstore.ensure_dirs(self.cfg)
        self.round_id = "20261011010101"
        self.old_pool = b"old-subscription-bytes\n"
        self.old_current = b'{"round_id":"old","nodes":[{"name":"old"}]}'
        with open(self.cfg.pool_path, "wb") as f:
            f.write(self.old_pool)
        current = self.cfg.sub("run/final/current.json")
        with open(current, "wb") as f:
            f.write(self.old_current)
        self.current_path = current

    def _fake_publish(self, cfg, nodes):
        with open(cfg.pool_path, "wb") as f:
            f.write(b"new-subscription-bytes\n")
        return cfg.pool_path

    def _fake_current(self, cfg, round_id, nodes):
        with open(cfg.sub("run/final/current.json"), "wb") as f:
            f.write(b'{"round_id":"new","nodes":[]}')

    def _assert_old_state(self):
        with open(self.cfg.pool_path, "rb") as f:
            self.assertEqual(f.read(), self.old_pool)
        with open(self.current_path, "rb") as f:
            self.assertEqual(f.read(), self.old_current)

    def test_snapshot_failure_rolls_back_pool_and_current(self):
        def partial_snapshot(cfg, round_id):
            hist = cfg.sub("run/history/pool")
            os.makedirs(hist, exist_ok=True)
            with open(os.path.join(hist, "partial_" + round_id + ".json"), "w") as f:
                f.write("{}")
            raise OSError("injected snapshot failure")

        with mock.patch.object(pipeline.poolstore, "emit_and_publish",
                               side_effect=self._fake_publish), \
             mock.patch.object(pipeline.poolstore, "write_current_final",
                               side_effect=self._fake_current), \
             mock.patch.object(pipeline.isolation, "snapshot_pool",
                               side_effect=partial_snapshot):
            with self.assertRaisesRegex(OSError, "injected snapshot failure"):
                pipeline._publish_pool_transaction(self.cfg, self.round_id, [])
        self._assert_old_state()
        self.assertFalse(os.path.exists(self.cfg.sub(
            "run/history/pool/partial_" + self.round_id + ".json")))

    def test_current_file_failure_rolls_back_subscription(self):
        with mock.patch.object(pipeline.poolstore, "emit_and_publish",
                               side_effect=self._fake_publish), \
             mock.patch.object(pipeline.poolstore, "write_current_final",
                               side_effect=OSError("injected current write failure")):
            with self.assertRaisesRegex(OSError, "injected current write failure"):
                pipeline._publish_pool_transaction(self.cfg, self.round_id, [])
        self._assert_old_state()

    def test_publish_failure_after_partial_replace_restores_old_pool(self):
        def partial_publish(cfg, nodes):
            with open(cfg.pool_path, "wb") as f:
                f.write(b"partial-new-subscription")
            raise OSError("injected publish failure")

        with mock.patch.object(pipeline.poolstore, "emit_and_publish",
                               side_effect=partial_publish):
            with self.assertRaisesRegex(OSError, "injected publish failure"):
                pipeline._publish_pool_transaction(self.cfg, self.round_id, [])
        self._assert_old_state()

    def test_first_publish_failure_removes_partial_new_files(self):
        os.remove(self.cfg.pool_path)
        os.remove(self.current_path)

        def partial_publish(cfg, nodes):
            with open(cfg.pool_path, "wb") as f:
                f.write(b"partial-new-subscription")
            raise OSError("injected first publish failure")

        with mock.patch.object(pipeline.poolstore, "emit_and_publish",
                               side_effect=partial_publish):
            with self.assertRaisesRegex(OSError, "injected first publish failure"):
                pipeline._publish_pool_transaction(self.cfg, self.round_id, [])
        self.assertFalse(os.path.exists(self.cfg.pool_path))
        self.assertFalse(os.path.exists(self.current_path))


if __name__ == "__main__":
    unittest.main()
