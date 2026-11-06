# Contributing

Thanks for helping. The point of this overlay is that an NCEA student gets a
grade that can be checked, not one that merely sounds right.

## Ground rules

1. **Never add exam paper text.** The question index stores a location
   (year / question / part), a verified answer and a verification level. A PR
   that pastes question text is rejected.
2. **Never add student data or deployment state.** No transcripts, no e-mail
   addresses, no hostnames, no API keys, no grants.
3. **A behaviour change needs a test.** The gate, the output guard and the audit
   rules decide whether a student is told they passed. A fix without a test that
   fails before and passes after is not merged.
4. **One concern per PR.** Small PRs merge quickly.

## Workflow

1. Open an issue describing the problem, or comment on an existing one.
2. Branch from `main`: `feat/<topic>` or `fix/<topic>`.
3. Run `python -m pytest tests -q` locally; CI runs the same command.
4. Open a pull request. `main` is protected: one approval and a green CI run are
   required, and direct pushes are rejected.

## Hot file

`scripts/apply_site_patches.py` is one file that patches the installed package,
so two people editing it at once will conflict. Mention in your issue that you
are touching it, and keep each change inside its own installer function so the
diff stays reviewable.

## Contribution terms

By opening a pull request you agree that your contribution is licensed under the
Apache License 2.0, on the same terms as the rest of this repository.
