import asyncio,json
from pathlib import Path
from dataclasses import replace
from sqlalchemy import select
from careerforge.application import Application
from careerforge.config import Settings
from careerforge.models import AgentRun,ModelReceipt
from careerforge.contracts_scope_binding_v166 import RangeRetryableScopedResearchReviewOutput
from careerforge.services.quote_correction import frozen_quote_schema, _without_ambiguous_references, CORRECTION_INSTRUCTIONS, QuotePatches, apply_quote_patches

async def main():
    path=Path("private/local-validation/quote-patch-live-20261117.json")
    if path.exists():raise SystemExit("Existing probe preserved")
    app=Application(replace(Settings.from_env(),mode="development",model_name="gpt-6.1-terra",model_streaming=True,model_timeout=300,database_url="sqlite:///private/local-runtime/development.db",state_dir=Path("private/local-runtime")))
    data=json.loads(Path("private/local-validation/pi-evidence-closure-v4-20261117.json").read_text(encoding="utf-8"))
    rid=next(r["id"] for r in data["runs"] if r["workflow"]=="review")
    with app.db.read() as db:
        run=db.get(AgentRun,rid)
        receipt=db.scalar(select(ModelReceipt).where(ModelReceipt.run_id==rid,ModelReceipt.stage_key.like("review.final:%"),~ModelReceipt.stage_key.like("%correction%")))
    original=receipt.response["structured"]
    bundle=app.runtime.stage(rid,"independent:"+receipt.stage_key.split(":",1)[1])
    reads={r["read_id"]:r["content"] for r in bundle["reads"]}
    issues=[]
    def scan(v,p=""):
        if isinstance(v,dict):
            if set(v)=={"read_id","quote"}:
                count=reads.get(v["read_id"],"").count(v["quote"])
                if count!=1:issues.append({"pointer":p,"match_count":count})
            else:
                for k,x in v.items():scan(x,p+"/"+k)
        elif isinstance(v,list):
            for i,x in enumerate(v):scan(x,p+"/"+str(i))
    scan(original)
    assert issues
    pointers=[x["pointer"] for x in issues]
    context={"quote_correction":{"original_response":original,"diagnostics":{"issues":issues}},"independent_bundle":{"reads":bundle["reads"]}}
    typ=RangeRetryableScopedResearchReviewOutput
    schema=QuotePatches.model_json_schema()
    schema["$defs"]["QuotePatch"]["properties"]["pointer"]["enum"]=pointers
    schema["$defs"]["ReadReference"]["properties"]["read_id"]["enum"]=list(reads)
    schema["properties"]["patches"].update(minItems=len(pointers),maxItems=len(pointers))
    request=app.gateway.request_payload("reviewer",CORRECTION_INSTRUCTIONS+"输出格式覆盖：仅返回 patches，每项 pointer 和 reference(read_id,quote)。只纠正诊断中指向的引用，不输出其他字段。",context,QuotePatches,output_schema=schema)
    record={"status":"running","probe_only":True,"original_run":rid,"issues":issues,"schema_bytes":len(json.dumps(schema)),"publication_written":False}
    path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    try:
        result=await app.provider.complete(request)
        record["response"]=result.model_dump(mode="json")
        result=result.model_copy(update={"structured":apply_quote_patches(original,QuotePatches.model_validate(result.structured),pointers)})
        record["merged_response"]=result.structured
        typ.model_validate(result.structured)
        assert _without_ambiguous_references(original,pointers)==_without_ambiguous_references(result.structured,pointers)
        issues.clear();scan(result.structured)
        assert not issues
        record.update(status="completed",identity_preserved=True,quotes_unique=True)
    except Exception as exc:record.update(status="failed",error=str(exc),code=getattr(exc,"code",type(exc).__name__))
    finally:
        path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({k:v for k,v in record.items() if k not in {"response","merged_response"}},ensure_ascii=False))
asyncio.run(main())
