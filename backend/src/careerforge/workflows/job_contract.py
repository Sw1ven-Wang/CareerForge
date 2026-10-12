from uuid import UUID

from careerforge.application.job_contract_service import VERSION, produce_report, read
from careerforge.workflows.shared.pure import execute_pure


def validate(db, run):
    for kind, key in [("snapshot", "job_snapshot_ids"), ("fact", "fact_ids")]:
        for identifier in run.input_manifest[key]:
            read(db, run.owner_id, UUID(identifier), kind)
    return {"input_manifest_hash": run.input_manifest_hash, "network_access": False}


def execute(run_id, worker):
    execute_pure(run_id, worker, VERSION, validate, produce_report)
