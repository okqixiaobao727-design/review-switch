#!/usr/bin/env python3
"""The caller's Response: what a resume must carry, and what the re-review reads.

The Response's one home in the suite as a *review* — the turn it changes, the
receipt it fills, the round it does or does not spend. What the command line
alone accepts and rejects is `test_bridge_arguments.py`'s.
"""

import contextlib
import copy
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from bridge_harness import DriverKilled, FakePaneTestCase


HEADING = "Caller's response to the previous round:"
INSTRUCTION = (
    "For each numbered finding, close it, or retain it against the stated "
    "reason. Report anything new only where a fix introduced it."
)
# One Response as a Dispatcher writes one: a line per finding, the finding
# named by its place in the report and a short quote, the decision after it.
RESPONSE_TEXT = (
    '1. "the guard runs after the lock" — fixed in bridge/review_bridge.py\n'
    '2. "the receipt omits the file" — declined: the receipt is out of scope\n'
    '3. "the reaper never runs" — deferred to review-switch#3\n'
)
LANES = ("codex", "claude")


class CallerResponseTestCase(FakePaneTestCase):
    """A spec lineage driven to its re-review, on either Lane."""

    def delivered_briefs(self, reviewer):
        """Every turn this Lane's reviewer received, its marker line stripped.

        The marker is one per call and is the Bridge finding its own turn
        again, not a line of the brief, so a test comparing two rounds' turns
        drops it.
        """
        prompts = (
            self.claude.prompts
            if reviewer == "claude"
            else [turn["input"][0]["text"] for turn in self.codex.started_turns]
        )
        return [prompt.split("\n", 1)[1] for prompt in prompts]

    def response_file(self, contents=RESPONSE_TEXT, name="response.md"):
        """A Response where the Dispatcher writes one: outside the checkout.

        Written as bytes so a test can put an exact line ending in the file and
        assert on it; text mode would rewrite it before the Bridge saw it.
        """
        path = self.root / name
        path.write_bytes(contents.encode("utf-8"))
        return str(path)

    def first_round(self, reviewer, message="one spec finding"):
        """Round one of a spec lineage, which answers nothing and carries no Response."""
        self.lane(reviewer).finish(message, axis="spec")
        code, output = self.run_bridge(
            self.args(reviewer=reviewer, axis="spec")
        )
        self.assertEqual(code, 0, output)
        return output["axes"]["spec"]["reviewSessionId"]

    def resume(self, reviewer, session, response, message="the fix closes it"):
        """The re-review, delivered the Response the caller wrote for it."""
        self.lane(reviewer).finish(message, axis="spec")
        return self.run_bridge(
            self.args(
                reviewer=reviewer,
                axis="spec",
                resume_session=session,
                response=response,
            )
        )


class ReReviewTurnTests(CallerResponseTestCase):
    """What the re-review's turn says, and what round one's does not."""

    def test_a_resume_delivers_the_unchanged_brief_then_the_response(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                session = self.first_round(reviewer)

                code, output = self.resume(
                    reviewer, session, self.response_file()
                )

                self.assertEqual(code, 0, output)
                briefs = self.delivered_briefs(reviewer)
                self.assertEqual(
                    briefs[-1],
                    f"{briefs[0]}\n\n{HEADING}\n{RESPONSE_TEXT}\n{INSTRUCTION}",
                )

    def test_a_first_review_carries_no_response_block(self):
        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                self.first_round(reviewer)

                brief = self.delivered_briefs(reviewer)[0]

                self.assertNotIn(HEADING, brief)
                self.assertNotIn(INSTRUCTION, brief)

    def test_the_instruction_scopes_what_a_re_review_may_newly_report(self):
        """The Rounds Contract's scope reaches the Lane only through this line.

        The brief above names the whole diff and never mentions rounds, so a
        re-review told nothing further sweeps that diff again and spends the
        one scoped round as a third. Spelled out here rather than read off the
        Bridge's constant: changing the sentence has to fail a test.
        """
        session = self.first_round("codex")

        code, output = self.resume("codex", session, self.response_file())

        self.assertEqual(code, 0, output)
        self.assertIn(
            "Report anything new only where a fix introduced it.",
            self.delivered_briefs("codex")[-1],
        )

    def test_a_response_written_with_crlf_arrives_unchanged(self):
        """Verbatim is bytes, not lines: text mode would fold CRLF to LF."""
        crlf = '1. "the guard" — fixed here\r\n2. "the receipt" — declined\r\n'
        session = self.first_round("codex")

        code, output = self.resume("codex", session, self.response_file(crlf))

        self.assertEqual(code, 0, output)
        self.assertIn(crlf, self.delivered_briefs("codex")[-1])
        self.assertEqual(
            pathlib.Path(output["preparation"]["responseFile"]).read_bytes(),
            crlf.encode("utf-8"),
        )

    def test_the_response_is_delivered_exactly_as_the_caller_wrote_it(self):
        """The Bridge parses no line of it, so an odd one arrives unchanged."""
        written = "  no numbers, no dashes, just this  "
        session = self.first_round("codex")

        code, output = self.resume(
            "codex", session, self.response_file(written)
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(
            self.delivered_briefs("codex")[-1],
            f"{self.delivered_briefs('codex')[0]}\n\n"
            f"{HEADING}\n{written}\n\n{INSTRUCTION}",
        )


class DocumentReviewResponseTests(CallerResponseTestCase):
    """A Document Review re-review through its public Next Call."""

    def test_design_re_review_requires_and_delivers_a_response_on_both_lanes(self):
        document = self.worktree / "docs/ticket.md"
        document.parent.mkdir(parents=True)
        document.write_text("Ticket.\n", encoding="utf-8")

        for reviewer in LANES:
            with self.subTest(reviewer=reviewer):
                first_args = self.parsed_args([
                    "--reviewer", reviewer,
                    "--cwd", str(self.worktree),
                    "--document", "docs/ticket.md",
                    "--axis", "design",
                    "--no-network",
                ])
                self.lane(reviewer).finish("one design finding", axis="design")
                first_code, first_output = self.run_bridge(first_args)
                self.assertEqual(first_code, 0, first_output)
                first_brief = self.delivered_briefs(reviewer)[-1]
                next_call = first_output["axes"]["design"]["nextCall"]
                argv_without_response = next_call["argv"][1:-2]

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaisesRegex(SystemExit, "2"):
                        self.bridge.parse_args(argv_without_response)

                self.assertIn(
                    "--resume-session requires --response",
                    stderr.getvalue(),
                )

                pathlib.Path(next_call["responseFile"]).write_text(
                    RESPONSE_TEXT,
                    encoding="utf-8",
                )
                resumed_args = self.parsed_args(next_call["argv"][1:])
                self.lane(reviewer).finish(
                    "the design response closes it",
                    axis="design",
                )
                resumed_code, resumed_output = self.run_bridge(resumed_args)

                self.assertEqual(resumed_code, 0, resumed_output)
                resumed_brief = self.delivered_briefs(reviewer)[-1]
                self.assertEqual(
                    resumed_brief,
                    f"{first_brief}\n\n{HEADING}\n{RESPONSE_TEXT}\n{INSTRUCTION}",
                )
                self.assertEqual(
                    resumed_output["preparation"],
                    {
                        **first_output["preparation"],
                        "responseFile": next_call["responseFile"],
                    },
                )
                resumed_result = resumed_output["axes"]["design"]
                self.assertEqual(resumed_result["next"], "escalate")
                self.assertIsNone(resumed_result["nextCall"])


class ResponseReceiptTests(CallerResponseTestCase):
    """`preparation.responseFile`: always present, and the file it names."""

    def test_the_receipt_names_the_response_kept_in_the_state_root(self):
        session = self.first_round("codex")

        code, output = self.resume("codex", session, self.response_file())

        self.assertEqual(code, 0, output)
        kept = pathlib.Path(output["preparation"]["responseFile"])
        self.assertTrue(kept.is_absolute())
        self.assertEqual(kept.parent, self.state_dir)
        self.assertEqual(kept.read_text(encoding="utf-8"), RESPONSE_TEXT)

    def test_the_next_call_names_an_absolute_response_file_from_a_relative_store(self):
        with contextlib.chdir(self.root), mock.patch.dict(
            os.environ,
            {"CODE_REVIEW_TUI_STATE_DIR": "relative-state"},
            clear=False,
        ):
            self.codex.finish("one spec finding", axis="spec")

            code, output = self.run_bridge(self.args(axis="spec"))

        self.assertEqual(code, 0, output)
        response_file = pathlib.Path(
            output["axes"]["spec"]["nextCall"]["responseFile"]
        )
        self.assertTrue(response_file.is_absolute())
        self.assertEqual(
            response_file.parent,
            (self.root / "relative-state").resolve(),
        )

    def test_the_result_named_response_file_is_not_copied_over_itself(self):
        self.codex.finish("one spec finding", axis="spec")
        first_code, first_output = self.run_bridge(self.args(axis="spec"))
        self.assertEqual(first_code, 0, first_output)
        first = first_output["axes"]["spec"]
        response_file = pathlib.Path(first["nextCall"]["responseFile"])
        response_file.write_text(RESPONSE_TEXT, encoding="utf-8")
        original_timestamp = 1_000_000_000
        os.utime(
            response_file,
            ns=(original_timestamp, original_timestamp),
        )

        code, output = self.resume(
            "codex",
            first["reviewSessionId"],
            str(response_file),
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(response_file.stat().st_mtime_ns, original_timestamp)
        self.assertIn(RESPONSE_TEXT, self.delivered_briefs("codex")[-1])

    def test_a_sibling_resume_cannot_leave_its_response_in_the_winners_receipt(self):
        """Preparation runs before the lock, so only the round's winner writes.

        The shape `grant_round` already guards against: two callers resume one
        handle, both prepare, and one takes the round. The one that takes it
        has to deliver and name the same Response — a receipt pointing at the
        loser's text is a human reading an escalation the reviewer never saw.
        """
        session = self.first_round("codex")
        sibling = '9. "a finding this caller never had" — declined\n'
        granted = self.bridge.grant_round

        def grant_round(args, owner, store):
            # The caller about to lose this round, writing into the window
            # preparation leaves open before the lock closes it.
            store.write_response(session, sibling)
            return granted(args, owner, store)

        self.enter(mock.patch.object(self.bridge, "grant_round", grant_round))

        code, output = self.resume("codex", session, self.response_file())

        self.assertEqual(code, 0, output)
        delivered = self.delivered_briefs("codex")[-1]
        self.assertIn(RESPONSE_TEXT, delivered)
        self.assertNotIn(sibling, delivered)
        self.assertEqual(
            pathlib.Path(output["preparation"]["responseFile"]).read_text(
                encoding="utf-8"
            ),
            RESPONSE_TEXT,
        )

    def test_no_record_ever_says_the_round_was_had_and_names_no_response(self):
        """The round and what it was prepared from go down in one write.

        Sampling every write rather than one point in the review, because the
        defect this pins is a window between two of them: a record that says a
        second round has been had while naming no Response is a spent round
        whose recovery cannot report the file it was held to.
        """
        session = self.first_round("codex")
        persisted = []
        written = self.bridge.SessionStore.write

        def write(store, session_id, state):
            if session_id == session:
                persisted.append(copy.deepcopy(state))
            return written(store, session_id, state)

        self.enter(mock.patch.object(self.bridge.SessionStore, "write", write))

        code, output = self.resume("codex", session, self.response_file())

        self.assertEqual(code, 0, output)
        spent = [state for state in persisted if state.get("rounds") == 2]
        self.assertTrue(spent, "the resume recorded no second round")
        for state in spent:
            self.assertEqual(
                state["preparation"]["responseFile"],
                output["preparation"]["responseFile"],
            )

    def test_a_first_review_names_no_response_file(self):
        self.codex.finish("one spec finding", axis="spec")

        code, output = self.run_bridge(self.args(axis="spec"))

        self.assertEqual(code, 0, output)
        self.assertIn("responseFile", output["preparation"])
        self.assertIsNone(output["preparation"]["responseFile"])

    def test_the_response_is_never_written_into_the_reviewed_checkout(self):
        """The Spec brief lists untracked files, so preparation adds none."""
        session = self.first_round("codex")
        before = self.untracked_files()

        code, output = self.resume("codex", session, self.response_file())

        self.assertEqual(code, 0, output)
        self.assertEqual(self.untracked_files(), before)

    def untracked_files(self):
        """The new files the Review Scope holds, read with the Scope's own command."""
        return subprocess.run(
            [
                "git",
                "-C",
                str(self.worktree),
                *self.bridge.ReviewScope.UNTRACKED_ARGUMENTS,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout


class ResponseRequirementTests(CallerResponseTestCase):
    """A resume without a Response spends nothing: no turn, no record, no round."""

    def main(self, *arguments):
        """One Bridge call the way a terminal makes it, command line and all."""
        argv = [
            "review_bridge.py",
            "--reviewer", "codex",
            "--cwd", str(self.worktree),
            *arguments,
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            try:
                code = self.bridge.main()
            except SystemExit as exit_status:
                code = exit_status.code
        printed = stdout.getvalue().strip()
        return code, json.loads(printed) if printed else None

    def resume_argv(self, session, *arguments):
        return (
            "--base", self.fixed_point,
            "--spec", "spec.md",
            "--axis", "spec",
            "--timeout", "5",
            "--startup-timeout", "5",
            "--resume-session", session,
            *arguments,
        )

    def test_a_forgotten_or_empty_response_costs_the_resume_nothing(self):
        session = self.first_round("codex")
        record = self.state_dir / f"{session}.json"
        before = record.read_bytes()
        turns = len(self.codex.started_turns)

        for arguments in (
            (),
            ("--response", self.response_file("", name="empty.md")),
            ("--response", self.response_file(" \n\t\n", name="blank.md")),
        ):
            with self.subTest(arguments=arguments):
                code, output = self.main(*self.resume_argv(session, *arguments))

                self.assertNotEqual(code, 0)
                self.assertIsNone(output)
                self.assertEqual(record.read_bytes(), before)
                self.assertEqual(len(self.codex.started_turns), turns)

        # The same resume, called again with the file, still has its round.
        self.codex.finish("the fix closes it", axis="spec")
        code, output = self.main(
            *self.resume_argv(session, "--response", self.response_file())
        )

        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.codex.started_turns), turns + 1)
        self.assertEqual(output["axes"]["spec"]["next"], "escalate")

    def test_a_response_emptied_after_the_command_line_read_it_spends_no_round(self):
        """The command line's check is the early answer, not the binding one.

        The file it approved is not necessarily the file that would be
        delivered: emptied in between, an unread Response would reach the
        reviewer and take the round. It is read once, where it is taken.
        """
        session = self.first_round("codex")
        path = self.response_file()
        record = self.state_dir / f"{session}.json"
        before = record.read_bytes()
        turns = len(self.codex.started_turns)
        refuse = self.bridge.refuse_resume_past_cap

        def refuse_resume_past_cap(args, owner, store):
            # The caller's own file, emptied in the window between the command
            # line reading it and the round being taken against it.
            pathlib.Path(path).write_bytes(b"  \n\t\n")
            return refuse(args, owner, store)

        self.enter(
            mock.patch.object(
                self.bridge, "refuse_resume_past_cap", refuse_resume_past_cap
            )
        )

        code, output = self.main(*self.resume_argv(session, "--response", path))

        self.assertNotEqual(code, 0)
        self.assertIsNone(output)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(len(self.codex.started_turns), turns)

    def test_a_health_probe_cannot_spend_a_lineages_re_review(self):
        """Refused on the command line, so the round and the record are untouched."""
        session = self.first_round("codex")
        record = self.state_dir / f"{session}.json"
        before = record.read_bytes()
        turns = len(self.codex.started_turns)

        code, output = self.main(
            "--probe",
            # The handle's own axis, so nothing but the probe rule can refuse
            # this: a mismatched axis is rejected before the round is reached
            # and would pass this test for the wrong reason.
            "--axis", "spec",
            "--resume-session", session,
            "--response", self.response_file(),
        )

        self.assertNotEqual(code, 0)
        self.assertIsNone(output)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(len(self.codex.started_turns), turns)

    def test_a_refused_resume_is_a_command_line_error_and_not_a_refusal(self):
        """`refused` is the round cap's word; a forgotten file must not borrow it."""
        session = self.first_round("codex")

        code, output = self.main(*self.resume_argv(session))

        self.assertEqual(code, 2)
        self.assertIsNone(output)

    def test_a_standards_resume_is_held_to_the_rule_too(self):
        """The rule attaches to the resume, not to the axis that earns a round."""
        self.codex.finish("naming findings", axis="standards")
        code, output = self.run_bridge(self.args(axis="standards"))
        self.assertEqual(code, 0, output)
        session = output["axes"]["standards"]["reviewSessionId"]

        code, output = self.main(
            "--base", self.fixed_point,
            "--spec", "spec.md",
            "--axis", "standards",
            "--resume-session", session,
        )

        self.assertNotEqual(code, 0)
        self.assertIsNone(output)


class ResponseAndTheRoundCapTests(CallerResponseTestCase):
    """The cap is the Rounds Contract's, and the Response leaves it where it was."""

    def test_a_second_spec_resume_with_a_response_is_still_refused(self):
        session = self.first_round("codex")

        granted, _output = self.resume("codex", session, self.response_file())
        refused, output = self.resume("codex", session, self.response_file())

        self.assertEqual(granted, 0)
        self.assertEqual(refused, 1)
        self.assertEqual(output["status"], "refused")
        self.assertIsNone(output["preparation"])
        self.assertEqual(output["axes"]["spec"]["next"], "escalate")


class RecoveredResponseTests(CallerResponseTestCase):
    """What a recovered re-review reports it was held to."""

    def test_a_recovered_resume_names_the_response_its_round_was_held_to(self):
        """The receipt is on disk from the moment the round is granted.

        A driver killed mid-re-review has already spent the round, so the
        record it leaves has to describe that round rather than the one before
        it — otherwise the recovery reports round one's Response, which is no
        Response at all.
        """
        session = self.first_round("codex")
        response = self.response_file()
        self.codex.error("spec", DriverKilled())
        with self.assertRaises(DriverKilled):
            self.run_bridge(
                self.args(
                    axis="spec", resume_session=session, response=response
                )
            )
        del self.codex.axis_errors["spec"]
        self.codex.finish("the fix closes it", axis="spec")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0, output)
        self.assertTrue(output["axes"]["spec"]["recovered"])
        kept = pathlib.Path(output["preparation"]["responseFile"])
        self.assertEqual(kept.parent, self.state_dir)
        self.assertEqual(kept.read_text(encoding="utf-8"), RESPONSE_TEXT)

    def test_recovering_a_record_older_than_responses_still_names_one(self):
        """A review prepared before #37 was held to no Response, and says so."""
        state = self.kill_the_driver()
        del state["preparation"]["responseFile"]
        (self.state_dir / f"{state['reviewSessionId']}.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.codex.finish("legacy review findings")

        code, output = self.run_bridge(self.args(recover_session=True))

        self.assertEqual(code, 0, output)
        self.assertIn("responseFile", output["preparation"])
        self.assertIsNone(output["preparation"]["responseFile"])


if __name__ == "__main__":
    unittest.main()
