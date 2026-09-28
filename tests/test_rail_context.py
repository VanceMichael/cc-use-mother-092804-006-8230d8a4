"""校验跨境班列箱货节点与异常责任的领域资料。"""

import unittest
from pathlib import Path

from src.rail_context import DOMAIN, context_fingerprint, load_context


class ContextTest(unittest.TestCase):
    def test_example_matches_domain_and_is_stable(self) -> None:
        value = load_context(Path("fixtures/context.json"))
        self.assertEqual(value["domain"], DOMAIN)
        self.assertEqual(len(context_fingerprint(value)), 64)
        self.assertGreaterEqual(len(value["constraints"]), 4)

    def test_wrong_domain_is_rejected(self) -> None:
        source = Path("fixtures/context.json")
        raw = source.read_text(encoding="utf-8").replace(DOMAIN, "other-domain", 1)
        temporary = Path("fixtures/.invalid-context.json")
        temporary.write_text(raw, encoding="utf-8")
        try:
            with self.assertRaisesRegex(ValueError, "领域标识不一致"):
                load_context(temporary)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
