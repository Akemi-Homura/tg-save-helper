from __future__ import annotations

import unittest

from src.commands import CommandError, parse_command


class CommandParsingTest(unittest.TestCase):
    def test_mixed_accepts_from_checkpoint(self) -> None:
        command = parse_command("/mixed https://t.me/source from https://t.me/source/123 force")

        self.assertIsNotNone(command)
        self.assertEqual(command.name, "/mixed")
        self.assertEqual(
            command.args,
            ("https://t.me/source", "from", "https://t.me/source/123", "force"),
        )

    def test_mixed_rejects_unread(self) -> None:
        with self.assertRaises(CommandError):
            parse_command("/mixed https://t.me/source unread")


if __name__ == "__main__":
    unittest.main()
