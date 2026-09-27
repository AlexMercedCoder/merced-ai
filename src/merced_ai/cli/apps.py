"""Typer applications for each command family; order here is the order in `--help`."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="merced-ai",
    help="Create OAP-backed bots that run through existing AI agent harnesses.",
    no_args_is_help=True,
)
harness_app = typer.Typer(help="Discover and inspect installed agent harnesses.")
profile_app = typer.Typer(help="Create, validate, and inspect Open Agent Profiles.")
bot_app = typer.Typer(help="Bind portable profiles to local harness preferences.")
session_app = typer.Typer(help="Inspect durable local conversation sessions.")
group_app = typer.Typer(help="Collaborate with multiple OAP bots in one conversation.")
inbox_app = typer.Typer(
    help="Review OAP state deltas (learned state proposed by harness sessions) before applying."
)
app.add_typer(harness_app, name="harness")
app.add_typer(profile_app, name="profile")
app.add_typer(bot_app, name="bot")
app.add_typer(session_app, name="session")
app.add_typer(group_app, name="group")
app.add_typer(inbox_app, name="inbox")
eval_app = typer.Typer(help="Run one profile and prompt on several harnesses and compare them.")
app.add_typer(eval_app, name="eval")
