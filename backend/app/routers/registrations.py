from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from app.models.registrations import IssuerRegistration, CredentialRegistration
from config import settings
from app.plugins import (
    AskarStorage,
    BitstringStatusList,
    PublisherRegistrar,
    Soup,
    OrgbookPublisher,
)
from app.security import check_api_key_header

router = APIRouter(prefix="/registrations", tags=["Registrations"])


@router.post("/issuers")
async def register_issuer(
    request_body: IssuerRegistration, authorized=Depends(check_api_key_header)
):
    registration = vars(request_body)
    did_document = await PublisherRegistrar().register_issuer(
        registration["name"],
        registration["scope"],
        registration["url"],
        registration["description"],
        registration["multikey"],
    )
    return JSONResponse(status_code=201, content=did_document)
    issuer = {
        "id": did_document["id"],
        "name": registration["name"],
        "scope": registration["scope"],
    }
    return JSONResponse(status_code=201, content=issuer)


@router.post("/credentials")
async def register_credential_type(
    request_body: CredentialRegistration, authorized=Depends(check_api_key_header)
):
    credential_registration = request_body.model_dump()

    # Create a new status list for this type of credential
    status_list_id = await BitstringStatusList().create(credential_registration)
    credential_registration["statusList"] = [status_list_id]
    await AskarStorage().replace(
        "credentialRegistration",
        credential_registration["type"],
        credential_registration,
    )

    credential_template = await PublisherRegistrar().template_credential(
        credential_registration
    )
    await AskarStorage().replace(
        "credentialTemplate",
        credential_registration["type"],
        credential_template,
    )

    await OrgbookPublisher().create_credential_type(credential_registration)
    return JSONResponse(status_code=201, content=credential_registration)
