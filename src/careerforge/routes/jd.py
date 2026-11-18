"""Private, lossless job-description documents and independently budgeted extraction."""

from fastapi import Depends

from ..contracts_jd_v166 import JDExtractionInput, JDImportInput


def register_routes(app, service, actor_dependency):
    @app.post("/api/v1/jds", status_code=201)
    def import_jd(body: JDImportInput, user=Depends(actor_dependency)):
        return service.jd.import_jd(user, body)

    @app.get("/api/v1/jds/{identifier}")
    def read_jd(identifier: str, user=Depends(actor_dependency)):
        return service.jd.read(user, identifier)

    @app.post("/api/v1/jds/extractions", status_code=202)
    def extract_jd(body: JDExtractionInput, user=Depends(actor_dependency)):
        return service.jd.start_extraction(user, body)
