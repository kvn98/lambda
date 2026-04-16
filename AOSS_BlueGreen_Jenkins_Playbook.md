# AOSS Blue/Green Index Lifecycle — Complete Jenkins Playbook
### AI Handoff Document | MLOps Platform Team
### Last Updated: April 2026

---

## DOCUMENT PURPOSE

This is a complete, self-contained handoff document for any AI assistant, engineer, or automation system taking over this work. It covers the full architecture, every design decision and its reasoning, and step-by-step operational instructions for implementing index switching and deletion for AWS OpenSearch Serverless (AOSS) using Jenkins as the sole orchestration layer.

Read this document top to bottom before writing any code or pipelines.

---

## SECTION 1 — PROBLEM STATEMENT

The application is an MCP-based knowledge retrieval service that queries an AOSS collection for vector/KNN search. When the underlying index must be replaced (new embedding model, schema change, full reindex), we need:

1. **Zero-downtime cutover** — new index is built and validated before the app touches it
2. **A stable, runtime-switchable pointer** — app does not need redeployment to change which index it queries
3. **A rollback mechanism** — if the new index is bad, revert instantly without data loss
4. **A controlled delete mechanism** — the old index persists after cutover and is only destroyed by an explicit, separate pipeline run after a retention window

AOSS does not support index aliases as a Terraform-manageable or application-transparent resource the same way self-managed OpenSearch/Elasticsearch does. This rules out the standard alias-flip pattern and requires an architectural substitute.

---

## SECTION 2 — ARCHITECTURE OVERVIEW

### 2.1 Blue/Green Index Model

Two physical indexes coexist in the AOSS collection at all times during the lifecycle:

```
AOSS Collection
├── direct-sage-index-x-blue    ← either active or inactive
└── direct-sage-index-x-green   ← either active or inactive
```

- Only one is "active" at any time — the one the application queries
- The other is either being populated, serving as a rollback target, or awaiting deletion
- Both always exist in Terraform config and AWS until the delete pipeline explicitly destroys one
- Cutover does not delete the old index — it only changes the pointer

### 2.2 SSM Parameter Store as the Alias Substitute

Because AOSS aliases are not manageable via Terraform and cannot serve as a stable runtime indirection layer, **AWS SSM Parameter Store is the architectural substitute for index aliases**.

The application reads a well-known SSM parameter at runtime to determine which physical index to query. The index name is never hardcoded in application config.

```
SSM Parameters:
/mlops/aoss/<collection-name>/active_index_name     → "direct-sage-index-x-blue"
/mlops/aoss/<collection-name>/rollback_index_name   → "direct-sage-index-x-green"
```

Switching indexes = Jenkins writes a new value to `active_index_name`. No app redeployment. No Terraform apply.

### 2.3 Terraform Scope — Infrastructure Only

Terraform owns:
- AOSS collection (via collection module)
- Both physical index resources (via index module, `for_each` over a map)
- SSM parameters — created on first apply, never overwritten on subsequent applies

**Critical Terraform constraint:** All SSM parameter resources that hold the active/rollback index names must have:

```hcl
lifecycle {
  ignore_changes = [value]
}
```

This is non-negotiable. Without `ignore_changes`, every `terraform apply` will revert whatever value Jenkins wrote back to the Terraform default. Terraform creates the SSM parameter. Jenkins owns all subsequent writes to it.

Terraform does NOT:
- Invoke any Lambda
- Flip SSM values
- Call the OpenSearch API
- Delete indexes post-cutover
- Manipulate Terraform state directly

### 2.4 Jenkins Scope — All Runtime Operations

Jenkins owns every imperative, stateful operation:
- Writing/updating SSM parameter values (cutover and rollback)
- Validating index health via AOSS HTTP API
- Deleting old indexes via AOSS HTTP API `DELETE /<index>`
- Sequencing all of the above with human approval gates

### 2.5 Lambda — Fully Removed

A Lambda function previously existed to handle SSM flips and OpenSearch API calls. It has been fully eliminated. Everything it did moves to Jenkins pipelines.

The following must be removed from the IaC repository entirely:
- The `OPENSEARCHLAMBDA/` module directory
- `aws_lambda_function` resource
- `aws_lambda_invocation` resource
- Lambda IAM execution role
- `archive_file` / S3 artifact references
- `null_resource` pip install provisioner
- `requirements.txt` and `handler.py`

The IaC repo becomes pure infrastructure after this removal. Jenkins owns all runtime operations.

### 2.6 Application-Side Requirement — CRITICAL

During debugging, `config.py` in the `knowledge_mcp` service was observed hardcoding:

```python
index_name = "direct-sage-index-blue"
```

**This completely defeats the SSM pointer mechanism.** The application must read the index name from SSM at startup or per-request. Until this is fixed, no blue/green cutover will have any effect on the application's actual behavior.

The MLE team must change this to read from SSM:

```python
import boto3

ssm = boto3.client('ssm')
param = ssm.get_parameter(Name='/mlops/aoss/<collection>/active_index_name')
index_name = param['Parameter']['Value']
```

---

## SECTION 3 — SSM PARAMETER DESIGN

| Parameter Path | Purpose | Who Creates It | Who Writes Updates | When |
|---|---|---|---|---|
| `/mlops/aoss/<collection>/active_index_name` | The index the app queries | Terraform (`ignore_changes` after) | Jenkins (Cutover pipeline, Rollback) | Every cutover and rollback |
| `/mlops/aoss/<collection>/rollback_index_name` | Previous active index, held for rollback | Terraform (`ignore_changes` after) | Jenkins (written before every cutover flip) | Before every cutover flip |

**Rollback pointer write order is critical:** Jenkins must write the current active value to `rollback_index_name` BEFORE writing the new value to `active_index_name`. This guarantees the rollback pointer is valid even if the flip succeeds but the smoke test fails and an auto-revert fires.

---

## SECTION 4 — TERRAFORM INDEX MODULE REQUIREMENTS

Both indexes are declared in the Terraform index module at all times. The `for_each` map includes both slots permanently. Example:

```hcl
module "aoss_indexes" {
  source = "./modules/aoss-index"

  indexes = {
    blue = {
      name       = "direct-sage-index-x-blue"
      knn        = true
      dimensions = 1536
    }
    green = {
      name       = "direct-sage-index-x-green"
      knn        = true
      dimensions = 1536
    }
  }

  active_ssm_path   = "/mlops/aoss/${var.collection_name}/active_index_name"
  rollback_ssm_path = "/mlops/aoss/${var.collection_name}/rollback_index_name"
}
```

SSM resources inside the module:

```hcl
resource "aws_ssm_parameter" "active_index" {
  name  = var.active_ssm_path
  type  = "String"
  value = "direct-sage-index-x-blue"   # initial default only

  lifecycle {
    ignore_changes = [value]   # Jenkins owns all subsequent writes
  }
}

resource "aws_ssm_parameter" "rollback_index" {
  name  = var.rollback_ssm_path
  type  = "String"
  value = "NONE"   # initial default only

  lifecycle {
    ignore_changes = [value]
  }
}
```

---

## SECTION 5 — IAM REQUIREMENTS

### Jenkins Agent Role Permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:PutParameter"
      ],
      "Resource": "arn:aws:ssm:<region>:<account>:parameter/mlops/aoss/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "aoss:APIAccessAll"
      ],
      "Resource": "arn:aws:aoss:<region>:<account>:collection/<collection-id>"
    },
    {
      "Effect": "Allow",
      "Action": [
        "sts:AssumeRole"
      ],
      "Resource": "<JENKINS_AOSS_ROLE_ARN>"
    }
  ]
}
```

### AOSS Data Access Policy

The Jenkins agent role must be declared as a principal in the AOSS collection's data access policy with at minimum:

- `aoss:ReadDocument`
- `aoss:WriteDocument`
- `aoss:DeleteCollectionItems`
- `aoss:DescribeIndex`
- `aoss:CreateIndex`

This is managed in Terraform in the collection module's access policy. Add the Jenkins role ARN to the principals list.

---

## SECTION 6 — AUTHENTICATING TO AOSS FROM JENKINS

AOSS requires AWS SigV4 signed HTTP requests. `curl` supports this natively via `--aws-sigv4`.

The recommended credential pattern is for Jenkins to assume the appropriate IAM role via STS at the start of each pipeline run, then use those short-lived credentials for all subsequent AWS and AOSS API calls.

```groovy
stage('Assume AWS Role') {
    steps {
        script {
            def creds = sh(
                script: """
                aws sts assume-role \
                  --role-arn '${JENKINS_AOSS_ROLE_ARN}' \
                  --role-session-name 'jenkins-aoss-pipeline' \
                  --output json
                """,
                returnStdout: true
            )
            def json = readJSON text: creds
            env.AWS_ACCESS_KEY_ID     = json.Credentials.AccessKeyId
            env.AWS_SECRET_ACCESS_KEY = json.Credentials.SecretAccessKey
            env.AWS_SESSION_TOKEN     = json.Credentials.SessionToken
        }
    }
}
```

All subsequent `curl` calls to AOSS use:

```bash
curl -s \
  --aws-sigv4 "aws:amz:${AWS_REGION}:aoss" \
  --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" \
  -H "x-amz-security-token: ${AWS_SESSION_TOKEN}" \
  "https://${AOSS_ENDPOINT}/..."
```

Do not use long-lived static credentials. The STS session approach ensures credentials rotate per pipeline run.

---

## SECTION 7 — PIPELINE DEFINITIONS

There are three Jenkins pipelines. Each is a separate Jenkinsfile.

---

### PIPELINE 1 — Deploy New Index (Provision)

**Purpose:** Provisions the inactive index slot so it is ready to receive data. Does not touch the active index. Does not flip SSM. Does not delete anything.

**Trigger:** Manual or on merge to main branch.

**Inputs (Jenkins parameters):**
- `COLLECTION_NAME`
- `AOSS_ENDPOINT`
- `AWS_REGION`
- `JENKINS_AOSS_ROLE_ARN`

**Stages:**

```
1. Assume AWS Role
2. Read Active Slot from SSM
3. Derive Inactive Slot Name
4. Terraform Apply (provisions both indexes if not already present)
5. Validate Inactive Index Exists in AOSS
6. Report — print inactive index name for data population step
```

**Full Jenkinsfile:**

```groovy
pipeline {
    agent any

    parameters {
        string(name: 'COLLECTION_NAME',      defaultValue: '', description: 'AOSS collection name')
        string(name: 'AOSS_ENDPOINT',        defaultValue: '', description: 'AOSS collection endpoint (no trailing slash)')
        string(name: 'AWS_REGION',           defaultValue: 'us-east-1')
        string(name: 'JENKINS_AOSS_ROLE_ARN',defaultValue: '', description: 'IAM role ARN Jenkins assumes')
    }

    environment {
        SSM_ACTIVE_PATH   = "/mlops/aoss/${params.COLLECTION_NAME}/active_index_name"
        SSM_ROLLBACK_PATH = "/mlops/aoss/${params.COLLECTION_NAME}/rollback_index_name"
    }

    stages {

        stage('Assume AWS Role') {
            steps {
                script {
                    def creds = sh(script: """
                        aws sts assume-role \
                          --role-arn '${params.JENKINS_AOSS_ROLE_ARN}' \
                          --role-session-name 'jenkins-deploy' \
                          --output json
                    """, returnStdout: true)
                    def json = readJSON text: creds
                    env.AWS_ACCESS_KEY_ID     = json.Credentials.AccessKeyId
                    env.AWS_SECRET_ACCESS_KEY = json.Credentials.SecretAccessKey
                    env.AWS_SESSION_TOKEN     = json.Credentials.SessionToken
                }
            }
        }

        stage('Read Active Slot') {
            steps {
                script {
                    env.ACTIVE_INDEX = sh(script: """
                        aws ssm get-parameter \
                          --name '${env.SSM_ACTIVE_PATH}' \
                          --query 'Parameter.Value' \
                          --output text
                    """, returnStdout: true).trim()

                    env.INACTIVE_SLOT  = env.ACTIVE_INDEX.endsWith('blue') ? 'green' : 'blue'
                    def base           = env.ACTIVE_INDEX.replaceAll('(blue|green)$', '')
                    env.INACTIVE_INDEX = base + env.INACTIVE_SLOT

                    echo "Active index:   ${env.ACTIVE_INDEX}"
                    echo "Inactive index: ${env.INACTIVE_INDEX}"
                }
            }
        }

        stage('Terraform Apply') {
            steps {
                dir('terraform/aoss') {
                    sh "terraform init"
                    sh "terraform apply -auto-approve"
                }
            }
        }

        stage('Validate Inactive Index Exists') {
            steps {
                script {
                    def status = sh(script: """
                        curl -s -o /dev/null -w "%{http_code}" \
                          --aws-sigv4 "aws:amz:${params.AWS_REGION}:aoss" \
                          --user "${env.AWS_ACCESS_KEY_ID}:${env.AWS_SECRET_ACCESS_KEY}" \
                          -H "x-amz-security-token: ${env.AWS_SESSION_TOKEN}" \
                          "https://${params.AOSS_ENDPOINT}/${env.INACTIVE_INDEX}"
                    """, returnStdout: true).trim()

                    if (status != '200') {
                        error("Inactive index '${env.INACTIVE_INDEX}' not found in AOSS. HTTP ${status}")
                    }
                    echo "Inactive index confirmed. Ready for data population."
                }
            }
        }

    }

    post {
        success {
            echo "Deploy complete. Populate data into: ${env.INACTIVE_INDEX}"
        }
        failure {
            echo "Deploy pipeline failed. Review logs above."
        }
    }
}
```

---

### PIPELINE 2 — Cutover (Switch Active Index)

**Purpose:** Flips the SSM active pointer from the current index to the newly populated index. Old index remains untouched in AOSS and Terraform. Auto-reverts SSM if smoke test fails.

**Trigger:** Manual only. Human approval gate required.

**Inputs (Jenkins parameters):**
- `COLLECTION_NAME`
- `AOSS_ENDPOINT`
- `AWS_REGION`
- `JENKINS_AOSS_ROLE_ARN`
- `MIN_DOC_THRESHOLD` (integer — new index must have at least this many documents)

**Stages:**

```
1. Assume AWS Role
2. Read Current Active and Derive Target
3. Pre-Cutover Validation — doc count check against MIN_DOC_THRESHOLD
4. Save Rollback Pointer — write current active to rollback SSM
5. Human Approval Gate
6. Flip Active SSM Pointer
7. Smoke Test — query new active index; auto-revert SSM if fails
8. Report
```

**Full Jenkinsfile:**

```groovy
pipeline {
    agent any

    parameters {
        string(name: 'COLLECTION_NAME',       defaultValue: '')
        string(name: 'AOSS_ENDPOINT',         defaultValue: '')
        string(name: 'AWS_REGION',            defaultValue: 'us-east-1')
        string(name: 'JENKINS_AOSS_ROLE_ARN', defaultValue: '')
        string(name: 'MIN_DOC_THRESHOLD',     defaultValue: '1000', description: 'Minimum documents new index must have')
    }

    environment {
        SSM_ACTIVE_PATH   = "/mlops/aoss/${params.COLLECTION_NAME}/active_index_name"
        SSM_ROLLBACK_PATH = "/mlops/aoss/${params.COLLECTION_NAME}/rollback_index_name"
    }

    stages {

        stage('Assume AWS Role') {
            steps {
                script {
                    def creds = sh(script: """
                        aws sts assume-role \
                          --role-arn '${params.JENKINS_AOSS_ROLE_ARN}' \
                          --role-session-name 'jenkins-cutover' \
                          --output json
                    """, returnStdout: true)
                    def json = readJSON text: creds
                    env.AWS_ACCESS_KEY_ID     = json.Credentials.AccessKeyId
                    env.AWS_SECRET_ACCESS_KEY = json.Credentials.SecretAccessKey
                    env.AWS_SESSION_TOKEN     = json.Credentials.SessionToken
                }
            }
        }

        stage('Read Current State') {
            steps {
                script {
                    env.ACTIVE_INDEX = sh(script: """
                        aws ssm get-parameter \
                          --name '${env.SSM_ACTIVE_PATH}' \
                          --query 'Parameter.Value' --output text
                    """, returnStdout: true).trim()

                    def base         = env.ACTIVE_INDEX.replaceAll('(blue|green)$', '')
                    def inactiveSlot = env.ACTIVE_INDEX.endsWith('blue') ? 'green' : 'blue'
                    env.TARGET_INDEX = base + inactiveSlot

                    echo "Current active: ${env.ACTIVE_INDEX}"
                    echo "Cutover target: ${env.TARGET_INDEX}"
                }
            }
        }

        stage('Pre-Cutover Validation') {
            steps {
                script {
                    def docCount = sh(script: """
                        curl -s \
                          --aws-sigv4 "aws:amz:${params.AWS_REGION}:aoss" \
                          --user "${env.AWS_ACCESS_KEY_ID}:${env.AWS_SECRET_ACCESS_KEY}" \
                          -H "x-amz-security-token: ${env.AWS_SESSION_TOKEN}" \
                          "https://${params.AOSS_ENDPOINT}/${env.TARGET_INDEX}/_count" \
                          | jq '.count'
                    """, returnStdout: true).trim().toInteger()

                    int threshold = params.MIN_DOC_THRESHOLD.toInteger()
                    if (docCount < threshold) {
                        error("Target index has ${docCount} documents. Minimum required: ${threshold}. Aborting cutover.")
                    }
                    echo "Document count: ${docCount} — threshold passed."
                }
            }
        }

        stage('Save Rollback Pointer') {
            steps {
                // Write BEFORE flipping active — this must happen first
                sh """
                aws ssm put-parameter \
                  --name '${env.SSM_ROLLBACK_PATH}' \
                  --value '${env.ACTIVE_INDEX}' \
                  --type String \
                  --overwrite
                """
                echo "Rollback pointer saved: ${env.ACTIVE_INDEX}"
            }
        }

        stage('Human Approval') {
            steps {
                input(
                    message: "Flip active index from '${env.ACTIVE_INDEX}' to '${env.TARGET_INDEX}'?",
                    ok: "Confirm Cutover"
                )
            }
        }

        stage('Flip Active Pointer') {
            steps {
                sh """
                aws ssm put-parameter \
                  --name '${env.SSM_ACTIVE_PATH}' \
                  --value '${env.TARGET_INDEX}' \
                  --type String \
                  --overwrite
                """
                echo "Active pointer flipped to: ${env.TARGET_INDEX}"
            }
        }

        stage('Smoke Test') {
            steps {
                script {
                    def status = sh(script: """
                        curl -s -o /dev/null -w "%{http_code}" \
                          --aws-sigv4 "aws:amz:${params.AWS_REGION}:aoss" \
                          --user "${env.AWS_ACCESS_KEY_ID}:${env.AWS_SECRET_ACCESS_KEY}" \
                          -H "x-amz-security-token: ${env.AWS_SESSION_TOKEN}" \
                          -X POST "https://${params.AOSS_ENDPOINT}/${env.TARGET_INDEX}/_search" \
                          -H 'Content-Type: application/json' \
                          -d '{"query":{"match_all":{}},"size":1}'
                    """, returnStdout: true).trim()

                    if (status != '200') {
                        echo "Smoke test failed (HTTP ${status}). Auto-reverting SSM pointer..."
                        sh """
                        aws ssm put-parameter \
                          --name '${env.SSM_ACTIVE_PATH}' \
                          --value '${env.ACTIVE_INDEX}' \
                          --type String \
                          --overwrite
                        """
                        error("Cutover aborted. SSM reverted to '${env.ACTIVE_INDEX}'. Investigate target index.")
                    }
                    echo "Smoke test passed. Cutover complete."
                }
            }
        }

    }

    post {
        success {
            echo """
            CUTOVER COMPLETE
            Active index : ${env.TARGET_INDEX}
            Rollback to  : ${env.ACTIVE_INDEX} (still exists in AOSS — do not delete yet)
            Run Pipeline 3 only after a retention window (recommended: 24-48h minimum).
            """
        }
        failure {
            echo "Cutover failed. Active index remains: ${env.ACTIVE_INDEX}"
        }
    }
}
```

---

### PIPELINE 3 — Delete Old Index

**Purpose:** Permanently deletes the inactive (old) index from AOSS after the retention window has passed and the new index is confirmed stable. After this pipeline runs successfully, the deleted index must also be removed from the Terraform config and `terraform apply` must be run.

**Trigger:** Manual only. Human approval gate required. Never run this immediately after cutover.

**Inputs (Jenkins parameters):**
- `COLLECTION_NAME`
- `AOSS_ENDPOINT`
- `AWS_REGION`
- `JENKINS_AOSS_ROLE_ARN`

**Stages:**

```
1. Assume AWS Role
2. Read Active and Rollback SSM Values
3. Safety Check — abort if rollback index matches active index
4. Human Approval Gate — shows exactly what will be permanently deleted
5. Delete Index via AOSS API
6. Clear Rollback SSM Pointer
7. Instruct engineer to remove from Terraform config
```

**Full Jenkinsfile:**

```groovy
pipeline {
    agent any

    parameters {
        string(name: 'COLLECTION_NAME',       defaultValue: '')
        string(name: 'AOSS_ENDPOINT',         defaultValue: '')
        string(name: 'AWS_REGION',            defaultValue: 'us-east-1')
        string(name: 'JENKINS_AOSS_ROLE_ARN', defaultValue: '')
    }

    environment {
        SSM_ACTIVE_PATH   = "/mlops/aoss/${params.COLLECTION_NAME}/active_index_name"
        SSM_ROLLBACK_PATH = "/mlops/aoss/${params.COLLECTION_NAME}/rollback_index_name"
    }

    stages {

        stage('Assume AWS Role') {
            steps {
                script {
                    def creds = sh(script: """
                        aws sts assume-role \
                          --role-arn '${params.JENKINS_AOSS_ROLE_ARN}' \
                          --role-session-name 'jenkins-delete' \
                          --output json
                    """, returnStdout: true)
                    def json = readJSON text: creds
                    env.AWS_ACCESS_KEY_ID     = json.Credentials.AccessKeyId
                    env.AWS_SECRET_ACCESS_KEY = json.Credentials.SecretAccessKey
                    env.AWS_SESSION_TOKEN     = json.Credentials.SessionToken
                }
            }
        }

        stage('Read SSM State') {
            steps {
                script {
                    env.ACTIVE_INDEX = sh(script: """
                        aws ssm get-parameter \
                          --name '${env.SSM_ACTIVE_PATH}' \
                          --query 'Parameter.Value' --output text
                    """, returnStdout: true).trim()

                    env.ROLLBACK_INDEX = sh(script: """
                        aws ssm get-parameter \
                          --name '${env.SSM_ROLLBACK_PATH}' \
                          --query 'Parameter.Value' --output text
                    """, returnStdout: true).trim()

                    echo "Active index:   ${env.ACTIVE_INDEX}  (will NOT be touched)"
                    echo "Rollback index: ${env.ROLLBACK_INDEX}  (candidate for deletion)"
                }
            }
        }

        stage('Safety Check') {
            steps {
                script {
                    if (env.ROLLBACK_INDEX == 'NONE' || env.ROLLBACK_INDEX.isEmpty()) {
                        error("ABORT: Rollback pointer is NONE. No index to delete.")
                    }
                    if (env.ROLLBACK_INDEX == env.ACTIVE_INDEX) {
                        error("ABORT: Rollback index matches active index. Cannot delete the active index.")
                    }
                    echo "Safety check passed. Target for deletion: ${env.ROLLBACK_INDEX}"
                }
            }
        }

        stage('Human Approval') {
            steps {
                input(
                    message: """
                    PERMANENT DELETION — THIS CANNOT BE UNDONE.

                    Index to delete : ${env.ROLLBACK_INDEX}
                    Active index    : ${env.ACTIVE_INDEX}  (untouched)

                    Confirm you have waited the full retention window and the active index is healthy.
                    """,
                    ok: "Delete Permanently"
                )
            }
        }

        stage('Delete Index') {
            steps {
                script {
                    def status = sh(script: """
                        curl -s -o /dev/null -w "%{http_code}" \
                          --aws-sigv4 "aws:amz:${params.AWS_REGION}:aoss" \
                          --user "${env.AWS_ACCESS_KEY_ID}:${env.AWS_SECRET_ACCESS_KEY}" \
                          -H "x-amz-security-token: ${env.AWS_SESSION_TOKEN}" \
                          -X DELETE "https://${params.AOSS_ENDPOINT}/${env.ROLLBACK_INDEX}"
                    """, returnStdout: true).trim()

                    if (status != '200') {
                        error("Delete request failed. HTTP ${status}. Index may still exist — investigate before retrying.")
                    }
                    echo "Index '${env.ROLLBACK_INDEX}' deleted successfully."
                }
            }
        }

        stage('Clear Rollback Pointer') {
            steps {
                sh """
                aws ssm put-parameter \
                  --name '${env.SSM_ROLLBACK_PATH}' \
                  --value 'NONE' \
                  --type String \
                  --overwrite
                """
                echo "Rollback pointer cleared."
            }
        }

    }

    post {
        success {
            echo """
            DELETE COMPLETE
            Deleted index : ${env.ROLLBACK_INDEX}
            Active index  : ${env.ACTIVE_INDEX}  (unchanged)

            REQUIRED FOLLOW-UP ACTIONS:
            1. Remove '${env.ROLLBACK_INDEX}' entry from the Terraform index map in the IaC repo
            2. Run 'terraform apply' to sync state with reality
            3. Terraform will remove the resource from state — no recreation will occur since the config entry is also gone
            """
        }
        failure {
            echo "Delete pipeline failed. Index may still exist. Review logs before retrying."
        }
    }
}
```

---

## SECTION 8 — POST-DELETE TERRAFORM CLEANUP

After Pipeline 3 succeeds, the deleted index still exists in Terraform state but no longer exists in AWS. The next `terraform plan` will show a drift. To resolve:

**Step 1 — Remove the deleted index from the Terraform map:**

```hcl
# Before (both slots declared):
indexes = {
  blue  = { name = "direct-sage-index-x-blue", ... }
  green = { name = "direct-sage-index-x-green", ... }
}

# After deleting green via Pipeline 3:
indexes = {
  blue = { name = "direct-sage-index-x-blue", ... }
  # green removed
}
```

**Step 2 — Run `terraform apply`:**

Terraform detects the resource is in state but no longer in config. It removes it from state. Because the resource is also gone from AWS, this results in no AWS API calls — purely a state cleanup. No index is recreated.

**Do not use `terraform state rm` as a substitute for this.** It leaves config and state out of sync. Always remove from config and apply together.

---

## SECTION 9 — COMPLETE LIFECYCLE STATE MACHINE

```
[New model / reindex required]
            │
            ▼
 ┌─────────────────────────────┐
 │  PIPELINE 1: Deploy         │
 │  - Terraform apply          │
 │  - Both indexes exist       │
 │  - Validate inactive exists │
 └─────────────┬───────────────┘
               │
               ▼
 [Data population by MLOps/batch job]
 [Vectors written into INACTIVE index]
               │
               ▼
 ┌─────────────────────────────┐
 │  PIPELINE 2: Cutover        │
 │  - Validate doc count       │
 │  - Save rollback pointer    │
 │  - Human approval gate      │
 │  - Flip SSM active pointer  │
 │  - Smoke test               │
 │    ↳ Fail: auto-revert SSM  │
 └─────────────┬───────────────┘
               │
 [RETENTION WINDOW — min 24-48h]
 [App runs against new index]
 [Old index still alive in AOSS]
 [Rollback possible at any time]
               │
               ▼
 ┌─────────────────────────────┐
 │  PIPELINE 3: Delete         │
 │  - Safety check             │
 │  - Human approval gate      │
 │  - DELETE old index via API │
 │  - Clear rollback pointer   │
 └─────────────┬───────────────┘
               │
               ▼
 [Engineer removes old index from Terraform config]
 [terraform apply cleans state]
               │
               ▼
 [System returns to single active index]
 [Inactive slot empty — ready for next cycle]
```

---

## SECTION 10 — ARCHITECTURAL DECISION RATIONALE

| Decision | Rationale |
|---|---|
| SSM Parameter Store instead of aliases | AOSS does not expose index aliases as a manageable resource in the same way self-managed OpenSearch does. SSM is the correct substitute: it is managed, auditable, IAM-controlled, and runtime-readable by the application. |
| `ignore_changes = [value]` on SSM resources | Terraform must create SSM params (correct path, type, initial value) but must never overwrite them on re-apply. Jenkins is the authoritative writer after initial provisioning. Without this, every Terraform apply reverts a Jenkins cutover. |
| Both indexes always declared in Terraform until explicit delete | Removing an index from Terraform config while it still exists in AWS creates state drift. Keep both slots in config until Pipeline 3 has confirmed successful deletion, then remove from config and apply. |
| Delete as a separate pipeline from cutover | Decoupling enforces a retention window. Combining delete with cutover eliminates the rollback window entirely and makes a bad cutover permanently destructive. |
| Rollback pointer written before active flip | Write order is a hard constraint. If the flip succeeds but the smoke test fails and auto-revert fires, the rollback pointer must already be in place. Writing it after the flip creates a race condition where revert logic runs without a valid rollback target. |
| Human approval gates on cutover and delete | These are irreversible or hard-to-reverse operations. A human gate prevents automation bugs from causing production outages or data loss without a conscious engineer decision. |
| Jenkins instead of Lambda | Lambda required infrastructure overhead (IAM role, ZIP packaging, S3 artifact, invocation plumbing, null_resource provisioners) for what is fundamentally a scripted operational workflow. Jenkins provides this natively with pipeline visibility, retries, manual gates, and audit logs, with no additional infrastructure cost. |
| STS assume-role per pipeline run | Short-lived credentials scoped to each pipeline run. No long-lived static credentials stored in Jenkins. Credentials expire at the end of the run. |
| No Terraform state manipulation by Jenkins | Jenkins never runs `terraform state` commands. Jenkins only calls AWS APIs: SSM (put-parameter) and AOSS HTTP API. This keeps Terraform state integrity entirely in TFC's hands. |

---

## SECTION 11 — PIPELINE VARIABLES REFERENCE

| Variable | Description | Set By |
|---|---|---|
| `COLLECTION_NAME` | AOSS collection name | Jenkins parameter |
| `AOSS_ENDPOINT` | AOSS collection endpoint URL (no trailing slash) | Jenkins parameter |
| `AWS_REGION` | AWS region (e.g. `us-east-1`) | Jenkins parameter |
| `JENKINS_AOSS_ROLE_ARN` | IAM role ARN Jenkins assumes for AWS operations | Jenkins parameter |
| `MIN_DOC_THRESHOLD` | Minimum document count for cutover validation | Jenkins parameter (Pipeline 2) |
| `SSM_ACTIVE_PATH` | `/mlops/aoss/<collection>/active_index_name` | Derived from COLLECTION_NAME |
| `SSM_ROLLBACK_PATH` | `/mlops/aoss/<collection>/rollback_index_name` | Derived from COLLECTION_NAME |

---

## SECTION 12 — KNOWN ISSUES AND OPEN ITEMS

| Issue | Status | Owner |
|---|---|---|
| `config.py` hardcodes `index_name = "direct-sage-index-blue"` — defeats SSM mechanism entirely | Open — must be fixed before cutover has any effect | MLE team |
| IRSA role max session duration is 1 hour — `get_os_client()` in `knowledge_mcp/clients/` likely caches the boto3 client at startup, causing 403s after 1 hour when credentials expire | Open | MLE team |
| `run_role_arn` for TFC run role is unresolved — needed for Terraform apply steps in Pipeline 1 | Open | MLOps platform team |
| Confirm which AOSS index settings are honored (`auto_expand_replicas`, `number_of_shards`, `number_of_replicas`) — AOSS manages infrastructure automatically and may silently ignore these | Open — verify via AWS CLI | MLOps platform team |

---

*End of document. This playbook is complete and self-contained. Any AI or engineer picking this up should have everything needed to implement and operate the full blue/green lifecycle.*
