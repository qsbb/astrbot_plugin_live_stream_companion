# -*- coding: utf-8 -*-
"""fork 发布前自检回归：本仓库必须始终保持 0 findings。"""

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "audit_fork", ROOT / "scripts" / "audit_fork.py"
)
assert _SPEC and _SPEC.loader
audit_fork = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit_fork)


class TestForkAudit(unittest.TestCase):
    def test_audit_is_clean(self):
        findings = audit_fork.run_audit(ROOT)
        self.assertEqual(findings, [], "fork 自检不允许有 findings：\n" + "\n".join(findings))

    def test_audit_detects_planted_private_reference(self):
        """自检本身要有效：临时放一个私有引用必须被抓出来。"""
        probe = ROOT / "tests" / "_audit_probe.py"
        probe.write_text("# astrbot_plugin_voice_hub 内部接线\n", encoding="utf-8")
        try:
            findings = audit_fork.run_audit(ROOT)
        finally:
            probe.unlink()
        self.assertTrue(
            any("astrbot_plugin_voice_hub" in item or "私有插件 id" in item for item in findings),
            f"植入的私有引用未被检出：{findings}",
        )


if __name__ == "__main__":
    unittest.main()
