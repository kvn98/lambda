import json
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth, helpers


def normalize_host(collection_endpoint: str) -> str:
    endpoint = collection_endpoint.strip()
    endpoint = endpoint.replace("https://", "").replace("http://", "")
    return endpoint.rstrip("/")


def get_opensearch_client(collection_endpoint: str, region: str) -> OpenSearch:
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials available for SigV4 signing")

    auth = AWSV4SignerAuth(credentials, region, "aoss")

    return OpenSearch(
        hosts=[{"host": normalize_host(collection_endpoint), "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
    )


def fake_embedding(size: int = 1024, seed: float = 0.001) -> list[float]:
    return [round(seed + ((i % 10) * 0.0001), 6) for i in range(size)]


def build_documents_docs(index_name: str) -> list[dict]:
    rows = [
        {
            "chunk_id": "doc-001",
            "content": "Atlas Desk dummy document about annuity replacement review and suitability checks.",
            "file_name": "annuity_replacement_review.pdf",
            "file_category": "policy",
            "business_unit": "Annuities",
            "document_owner": "Atlas Desk QA",
            "url": "https://example.internal/atlas-desk/docs/doc-001",
            "last_modified_at": "2026-04-09 09:00:00",
            "embedding": fake_embedding(seed=0.001),
        },
        {
            "chunk_id": "doc-002",
            "content": "Atlas Desk dummy document describing underwriting workflow, escalation, and case routing.",
            "file_name": "underwriting_workflow_guide.docx",
            "file_category": "workflow",
            "business_unit": "Insurance",
            "document_owner": "Operations Test",
            "url": "https://example.internal/atlas-desk/docs/doc-002",
            "last_modified_at": "2026-04-09 09:05:00",
            "embedding": fake_embedding(seed=0.002),
        },
        {
            "chunk_id": "doc-003",
            "content": "Atlas Desk dummy document for customer service retrieval around beneficiary updates.",
            "file_name": "beneficiary_update_faq.txt",
            "file_category": "faq",
            "business_unit": "Service",
            "document_owner": "Knowledge Team",
            "url": "https://example.internal/atlas-desk/docs/doc-003",
            "last_modified_at": "2026-04-09 09:10:00",
            "embedding": fake_embedding(seed=0.003),
        },
    ]
    return [{"_index": index_name, "_id": row["chunk_id"], "_source": row} for row in rows]


def build_knowledge_docs(index_name: str) -> list[dict]:
    rows = [
        {
            "chunk_id": "know-001",
            "content": "Knowledge note for Atlas Desk on suitability policy interpretation and review thresholds.",
            "file_name": "suitability_policy_note.md",
            "file_category": "knowledge",
            "business_unit": "Wealth",
            "knowledge_domain": "Suitability",
            "url": "https://example.internal/atlas-desk/knowledge/know-001",
            "source_system": "sharepoint",
            "last_modified_at": "2026-04-09 09:15:00",
            "embedding": fake_embedding(seed=0.004),
        },
        {
            "chunk_id": "know-002",
            "content": "Knowledge note covering exception handling, fallback guidance, and escalation paths.",
            "file_name": "exception_handling_runbook.md",
            "file_category": "knowledge",
            "business_unit": "Operations",
            "knowledge_domain": "Runbooks",
            "url": "https://example.internal/atlas-desk/knowledge/know-002",
            "source_system": "confluence",
            "last_modified_at": "2026-04-09 09:20:00",
            "embedding": fake_embedding(seed=0.005),
        },
        {
            "chunk_id": "know-003",
            "content": "Knowledge note for document classification labels used by Atlas Desk retrieval.",
            "file_name": "classification_reference.md",
            "file_category": "reference",
            "business_unit": "Compliance",
            "knowledge_domain": "Taxonomy",
            "url": "https://example.internal/atlas-desk/knowledge/know-003",
            "source_system": "servicenow",
            "last_modified_at": "2026-04-09 09:25:00",
            "embedding": fake_embedding(seed=0.006),
        },
    ]
    return [{"_index": index_name, "_id": row["chunk_id"], "_source": row} for row in rows]


def load_docs_from_event(event: dict) -> list[dict]:
    if "documents" in event:
        docs = event["documents"]
        if not isinstance(docs, list) or not docs:
            raise ValueError("event.documents must be a non-empty list")
        normalized = []
        index_name = event["index_name"]
        for item in docs:
            source = dict(item)
            source.setdefault("embedding", fake_embedding())
            if "chunk_id" not in source:
                raise ValueError("Each document must include chunk_id")
            normalized.append({"_index": index_name, "_id": source["chunk_id"], "_source": source})
        return normalized

    profile = event.get("profile", "documents")
    index_name = event["index_name"]

    if profile == "knowledge":
        return build_knowledge_docs(index_name)
    return build_documents_docs(index_name)


def handler(event, context):
    collection_endpoint = event["collection_endpoint"]
    index_name = event["index_name"]
    region = event["region"]

    client = get_opensearch_client(collection_endpoint, region)
    docs = load_docs_from_event(event)

    success, failures = helpers.bulk(client, docs, raise_on_error=False)

    return {
        "statusCode": 200,
        "index_name": index_name,
        "requested": len(docs),
        "inserted": success,
        "failed_count": len(failures),
        "failures": failures[:3],
    }
