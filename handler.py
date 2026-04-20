import json
import logging
import os
from typing import Any

import boto3
import requests
from requests_aws4auth import AWS4Auth

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ["AWS_REGION"]
COLLECTION_ENDPOINT = os.environ["COLLECTION_ENDPOINT"]  # e.g. https://xxx.us-east-1.aoss.amazonaws.com
SERVICE = "aoss"


def get_auth() -> AWS4Auth:
    """Build SigV4 auth from the Lambda's execution role credentials."""
    credentials = boto3.Session().get_credentials()
    return AWS4Auth(
        credentials.access_key,
        credentials.secret_key,
        REGION,
        SERVICE,
        session_token=credentials.token,
    )


def check_index_exists(index_name: str, auth: AWS4Auth) -> bool:
    """
    Verify a physical index exists on the collection.
    Uses HEAD <index> — returns 200 if exists, 404 if not.
    """
    url = f"{COLLECTION_ENDPOINT}/{index_name}"
    logger.info(f"Checking index exists: {index_name}")
    resp = requests.head(url, auth=auth, timeout=10)

    if resp.status_code == 200:
        logger.info(f"Index {index_name} exists")
        return True
    if resp.status_code == 404:
        logger.warning(f"Index {index_name} does NOT exist")
        return False

    # Any other status (403, 500, etc.) — don't assume, surface the error
    raise RuntimeError(
        f"Unexpected status checking index {index_name}: "
        f"{resp.status_code} {resp.text}"
    )


def swap_alias(alias_name: str, old_index: str, new_index: str, auth: AWS4Auth) -> dict[str, Any]:
    """
    Atomic alias swap via POST _aliases.
    Removes alias from old_index and adds it to new_index in a single transaction.
    """
    url = f"{COLLECTION_ENDPOINT}/_aliases"
    body = {
        "actions": [
            {"remove": {"index": old_index, "alias": alias_name}},
            {"add": {"index": new_index, "alias": alias_name}},
        ]
    }

    logger.info(f"Swapping alias {alias_name}: {old_index} -> {new_index}")
    resp = requests.post(
        url,
        auth=auth,
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"Alias swap failed: {resp.status_code} {resp.text}"
        )

    logger.info(f"Alias swap successful: {resp.json()}")
    return resp.json()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Expected event shape:
    {
      "alias_name": "suitability-iq-active",
      "old_index": "suitability-iq-v1",
      "new_index": "suitability-iq-v2"
    }
    """
    alias_name = event["alias_name"]
    old_index = event["old_index"]
    new_index = event["new_index"]

    auth = get_auth()

    # Preflight: new index must exist before we point an alias at it
    if not check_index_exists(new_index, auth):
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": f"New index {new_index} does not exist — aborting swap",
            }),
        }

    # Execute the atomic swap
    try:
        result = swap_alias(alias_name, old_index, new_index, auth)
    except RuntimeError as e:
        logger.exception("Alias swap failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": f"Alias {alias_name} swapped from {old_index} to {new_index}",
            "aoss_response": result,
        }),
    }