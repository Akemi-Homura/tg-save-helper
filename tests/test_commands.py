from __future__ import annotations

import unittest

from src.commands import CommandError, HELP_TEXT, parse_command


class CommandParsingTest(unittest.TestCase):
    def test_saved_commands_accept_shared_selectors(self) -> None:
        cases = {
            "/syncsaved all": ("/syncsaved", ("all",)),
            "/streamsaved 25 force": ("/streamsaved", ("25", "force")),
            "/watchsaved from 123": ("/watchsaved", ("from", "123")),
            "/watchstreamsaved from": ("/watchstreamsaved", ("from",)),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertEqual((command.name, command.args), expected)

    def test_saved_commands_reject_invalid_selector(self) -> None:
        with self.assertRaises(CommandError):
            parse_command("/watchsaved from 123 456")

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

    def test_help_documents_mixed_checkpoint(self) -> None:
        self.assertIn(
            "/mixed <source> <count|all|from <message_link>> [force]",
            HELP_TEXT,
        )


if __name__ == "__main__":
    unittest.main()
