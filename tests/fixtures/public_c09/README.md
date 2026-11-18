# Public C09 source-fidelity fixtures

This is the minimal four-file subset of the user-provided 2026-11-05 integration v1 `agent_input` needed by `tests/test_public_question_adapter_v166.py`. `PROVENANCE.json` records the exact original byte counts and SHA-256 values. All eight original cases remain in their original order, including question, submission and help declaration. No Gold, holdout, expected grading answer or native model response is included.

The two Python files are explicitly synthetic lab inputs. Tests read them as text; they do not execute them or create learner execution receipts. The portable helper builds deterministic setup and checks field fidelity only. These fixtures do not establish model semantic correctness, production behavior or learning benefit.

From the source root, run:

```sh
python scripts/run_tests.py --output-dir validation/c09-source-fidelity --label portable tests/test_public_question_adapter_v166.py
```

Use a new output directory for each run. The loader validates the packaged bytes against the provenance record before reading cases.
