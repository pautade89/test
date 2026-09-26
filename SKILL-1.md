import os
import zipfile

FILES = {}

# =====================================================================
# 1. INSTRUCTIONS
# =====================================================================
FILES[".github/copilot-instructions.md"] = """# Master Instructions: rbc_api Ecosystem
- You are the master AI coordinator named `rbc_api`.
- You govern end-to-end XML API automated testing for CCMS and Synergy across QA and load-balanced UAT (UAT1 and UAT2).
- All requests use HTTP POST with XML payloads (`Content-Type: application/xml`).
- Always route user requests to appropriate specialized sub-agents.
- Before inventing any payload schema or formula, query the `domain-knowledge-server` MCP server.
"""

FILES[".github/instructions/ccms.instructions.md"] = """# CCMS Module Behavioral Instructions
applyTo: "tests/ccms/**,knowledge/ccms/**"

- Root XML envelope: `<CCMSRequest>` and `<CCMSResponse>`.
- Mandatory XML Headers: `<Header><SecurityToken>{session_token}</SecurityToken></Header>`.
- Allowed Case Lifecycle States: DRAFT, SUBMITTED, UNDER_REVIEW, APPROVED, REJECTED.
- Idempotency Rule: Repeated POST with identical `<CaseId>` returns HTTP 409 or `<Status>DUPLICATE</Status>`.
- Calculation Engine: Overdue penalties must be calculated via `CCMS_FORMULA_01` (calls `FormulaEngine.calculate_ccms_penalty`).
- Environment endpoints:
  - QA: `https://qa.api.internal/ccms`
  - UAT1: `https://uat1.api.internal/ccms`
  - UAT2: `https://uat2.api.internal/ccms`
"""

FILES[".github/instructions/synergy.instructions.md"] = """# Synergy Module Behavioral Instructions
applyTo: "tests/synergy/**,knowledge/synergy/**"

- Root XML envelope: `<SynergyRequest>` and `<SynergyResponse>`.
- Mandatory Authentication: `<Auth><ApiKey>{api_key}</ApiKey></Auth>`.
- Entity Classifications: INDIVIDUAL, CORPORATE, HYBRID.
- Risk Rule: If `<ExposureAmount>` > 1,000,000, `<RiskScore>` tag (0 to 100) is strictly mandatory.
- Calculation Engine: Exposure calculation must follow `SYN_FORMULA_01` (calls `FormulaEngine.calculate_synergy_exposure`).
- Load-Balanced UAT Verification: Every response contains header `X-Backend-Node` (`UAT1` or `UAT2`). Tests must assert node health.
"""

# =====================================================================
# 2. AGENTS
# =====================================================================
FILES[".github/agents/rbc_api.agent.md"] = """# Master Agent: rbc_api
Role: Central Coordinator & Supervisor for CCMS & Synergy QA Automation.

## Routing Matrix:
1. User asks about CCMS rules/schemas -> Delegate to `@ccms-domain`.
2. User asks about Synergy rules/schemas -> Delegate to `@synergy-domain`.
3. User asks to design scenarios or matrices -> Delegate to `@ccms-test-design` or `@synergy-test-design`.
4. User provides Pytest failures or Streamlit logs -> Delegate to `@failure-rca`.

## Guidelines:
- Never answer domain-specific calculations using unverified LLM math. Always enforce using the deterministic FormulaEngine.
- Ensure all test artifacts fit into the Pytest runner and Streamlit dashboard interface.
"""

FILES[".github/agents/ccms-domain.agent.md"] = """# Sub-Agent: ccms-domain
Role: Custodian of CCMS business rules, XML schema specifications, and state transitions.

## Skills Used:
- `skills/domain-knowledge/SKILL.md`

## Instructions:
- Query MCP memory (`get_business_rules(module="ccms")`) to validate XML structure.
- Validate penalty calculations against `config/formulas/business_formulas.yaml`.
- Enforce strict state transitions (e.g., DRAFT cannot transition directly to APPROVED).
"""

FILES[".github/agents/synergy-domain.agent.md"] = """# Sub-Agent: synergy-domain
Role: Custodian of Synergy business rules, exposure models, and node routing.

## Skills Used:
- `skills/domain-knowledge/SKILL.md`

## Instructions:
- Query MCP memory (`get_business_rules(module="synergy")`).
- Validate entity classification and ensure risk score boundaries (0-100) are respected.
- Handle dual-server UAT scenarios (session affinity and database synchronization verification).
"""

FILES[".github/agents/ccms-test-design.agent.md"] = """# Sub-Agent: ccms-test-design
Role: Generates test matrices and executable Pytest XML test cases for CCMS.

## Skills Used:
- `skills/test-scenario-design/SKILL.md`
- `skills/testcase-generation/SKILL.md`

## Output Standard:
- Test files placed under `tests/ccms/test_<feature>.py`.
- Must use `@pytest.mark.parametrize` and XPath response parsing.
"""

FILES[".github/agents/synergy-test-design.agent.md"] = """# Sub-Agent: synergy-test-design
Role: Generates test matrices and executable Pytest XML test cases for Synergy.

## Skills Used:
- `skills/test-scenario-design/SKILL.md`
- `skills/testcase-generation/SKILL.md`

## Output Standard:
- Test files placed under `tests/synergy/test_<feature>.py`.
- Include dual-server tests running across both UAT1 and UAT2 endpoints.
"""

FILES[".github/agents/failure-rca.agent.md"] = """# Sub-Agent: failure-rca
Role: Autonomous Triager and Root-Cause Analyzer for Pytest and Streamlit execution failures.

## Skills Used:
- `skills/failure-rca/SKILL.md`

## Responsibilities:
- Parse failed XML responses, HTTP status codes, and server headers.
- Compare actual numeric outputs against `FormulaEngine` ground truths.
- Update permanent memory in `.qa_memory/failure_history.sqlite` with identified bug patterns.
"""

# =====================================================================
# 3. SKILLS
# =====================================================================
FILES[".github/skills/domain-knowledge/SKILL.md"] = """# Skill: Domain Knowledge Retrieval & Verification
1. Access the MCP server `domain-knowledge-server` before generating domain assertions.
2. Cross-verify incoming XML nodes against schema elements stored in `knowledge/`.
3. Check status codes against business errors (CCMS: `ERR_xxx`, Synergy: `SYN_xxx`).
"""

FILES[".github/skills/test-scenario-design/SKILL.md"] = """# Skill: Test Scenario Matrix Design
Every scenario matrix must include:
1. **Happy Path**: Valid XML with nominal boundary data.
2. **Boundary/Formula Stress**: Limits on numeric values and dates.
3. **Negative/Fault Injection**: Missing required nodes, malformed XML strings.
4. **Idempotency/Concurrency**: Duplicate identifiers on QA, UAT1, and UAT2.
"""

FILES[".github/skills/testcase-generation/SKILL.md"] = """# Skill: Pytest XML Testcase Generation
1. Method is always HTTP POST with `Content-Type: application/xml`.
2. Use XPath (`xml.etree.ElementTree` or `lxml`) for all assertions; do not use substring matching.
3. Import `FormulaEngine` from `framework.calculators.evaluator` for numeric assertions.
4. Integrate with `tests/conftest.py` fixtures: `env_config`, `send_xml_post`.
"""

FILES[".github/skills/failure-rca/SKILL.md"] = """# Skill: Failure Root Cause Analysis
1. Extract HTTP status code, request XML, and response XML from Pytest failure report.
2. Identify failure class:
   - `SCHEMA_VIOLATION`: Missing tags, malformed syntax.
   - `BUSINESS_RULE_VIOLATION`: Invalid case transition, unhandled risk level.
   - `FORMULA_MISMATCH`: Calculation divergence from `FormulaEngine`.
   - `ENVIRONMENT_DRIFT`: Error reproducible on UAT2 but passing on UAT1.
3. Propose exact fixes and log pattern to MCP memory.
"""

# =====================================================================
# 4. PROMPTS
# =====================================================================
FILES[".github/prompts/ccms-domain-analysis.prompt.md"] = """# Prompt: CCMS Domain Analysis
Review the requested feature: `{{feature_description}}`.
Query MCP memory for CCMS rules and return:
1. Mandatory XML tags.
2. Permitted state transitions.
3. Applicable formula IDs and input/output XPaths.
"""

FILES[".github/prompts/synergy-domain-analysis.prompt.md"] = """# Prompt: Synergy Domain Analysis
Review the requested feature: `{{feature_description}}`.
Query MCP memory for Synergy rules and return:
1. Entity classification checks.
2. Risk threshold requirements.
3. Expected node response headers on UAT1 and UAT2.
"""

FILES[".github/prompts/ccms-test-scenario-generation.prompt.md"] = """# Prompt: CCMS Test Scenario Generation
Generate a comprehensive scenario matrix in Markdown table format for:
Module: CCMS
Feature: `{{feature_name}}`
Environments: QA, UAT1, UAT2
Include: Scenario ID, Description, XML Input Variation, Expected Status, Expected XPath.
"""

FILES[".github/prompts/synergy-test-scenario-generation.prompt.md"] = """# Prompt: Synergy Test Scenario Generation
Generate a comprehensive scenario matrix in Markdown table format for:
Module: Synergy
Feature: `{{feature_name}}`
Environments: QA, UAT1, UAT2
Include: Load-balancing behavior between UAT1 and UAT2 nodes.
"""

FILES[".github/prompts/ccms-testcase-generation.prompt.md"] = """# Prompt: CCMS Pytest Code Generation
Generate an executable Pytest script for CCMS scenario `{{scenario_id}}`.
Requirements:
- Target: `tests/ccms/test_{{feature_name}}.py`
- Request: XML constructed for `<CCMSRequest>`
- Assertions: XPath queries on `<CCMSResponse>`
- Expected values: Derived dynamically using `FormulaEngine` where applicable.
"""

FILES[".github/prompts/synergy-testcase-generation.prompt.md"] = """# Prompt: Synergy Pytest Code Generation
Generate an executable Pytest script for Synergy scenario `{{scenario_id}}`.
Requirements:
- Target: `tests/synergy/test_{{feature_name}}.py`
- Include UAT node validation (`UAT1` and `UAT2`).
- Assertions: XPath checks on `<SynergyResponse>`.
"""

FILES[".github/prompts/failure-rca.prompt.md"] = """# Prompt: Test Failure Root-Cause Analysis
Diagnose the following test failure:
- Environment: `{{env_name}}`
- Target Node: `{{node_id}}`
- Request XML: `{{request_xml}}`
- Response XML: `{{response_xml}}`
- Stack Trace: `{{stack_trace}}`

Classify root cause and provide actionable remediation code.
"""

# =====================================================================
# 5. HOOKS & MCP MEMORY SERVER
# =====================================================================
FILES[".github/hooks/post-test-run.sh"] = """#!/usr/bin/env bash
# Triggered automatically after Pytest execution from Streamlit/CLI
echo "Pytest run complete. Inspecting test-reports for failures..."
if [ -f "test-reports/report.json" ]; then
    python -m mcp.domain-knowledge-server.triage_hook --report test-reports/report.json
fi
"""

FILES["mcp/domain-knowledge-server/requirements.txt"] = """mcp>=1.0.0
chromadb>=0.5.0
pydantic>=2.0.0
pyyaml>=6.0
networkx>=3.0
"""

FILES["mcp/domain-knowledge-server/server.py"] = """import json
import sqlite3
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Domain-Knowledge-Server")

MEMORY_DIR = Path("memory")
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
GRAPH_FILE = MEMORY_DIR / "relationships" / "graph.jsonl"
GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
SQLITE_DB = MEMORY_DIR / "failure_history.sqlite"

def init_db():
    with sqlite3.connect(SQLITE_DB) as conn:
        conn.execute(\"\"\"
            CREATE TABLE IF NOT EXISTS failure_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT,
                environment TEXT,
                error_code TEXT,
                root_cause TEXT,
                resolution TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        \"\"\")
init_db()

@mcp.tool()
def get_business_rules(module: str, topic: str = "") -> str:
    \"\"\"Query business rules and state machines for CCMS or Synergy.\"\"\"
    module_rules = {
        "ccms": "Envelope: <CCMSRequest>. States: DRAFT->SUBMITTED->UNDER_REVIEW->APPROVED/REJECTED. Token mandatory.",
        "synergy": "Envelope: <SynergyRequest>. Entities: INDIVIDUAL, CORPORATE, HYBRID. Risk score mandatory if Exposure > 1M."
    }
    return module_rules.get(module.lower(), "Unknown module specified.")

@mcp.tool()
def query_graph_relations(module: str) -> str:
    \"\"\"Fetch semantic entity relationships from persistent graph storage.\"\"\"
    if not GRAPH_FILE.exists():
        return "No relationships currently stored."
    matches = []
    with open(GRAPH_FILE, "r", encoding="utf-8") as f:
        for line in f:
            item = json.loads(line)
            if item.get("module", "").lower() == module.lower():
                matches.append(f"{item['source']} --[{item['relation']}]--> {item['target']}")
    return "\\n".join(matches) if matches else "No matching relations."

@mcp.tool()
def log_failure_analysis(module: str, env: str, error_code: str, root_cause: str, resolution: str) -> str:
    \"\"\"Save RCA learnings into permanent memory.\"\"\"
    with sqlite3.connect(SQLITE_DB) as conn:
        conn.execute(\"\"\"
            INSERT INTO failure_history (module, environment, error_code, root_cause, resolution)
            VALUES (?, ?, ?, ?, ?)
        \"\"\", (module.upper(), env.upper(), error_code, root_cause, resolution))
    return f"Failure logged for {module} on {env}."

if __name__ == "__main__":
    mcp.run()
"""

# =====================================================================
# 6. CONFIG, ENGINE & PYTEST HOOKS
# =====================================================================
FILES["config/formulas/business_formulas.yaml"] = """ccms:
  CCMS_FORMULA_01:
    name: "overdue_penalty"
    formula: "penalty_rate * overdue_days * risk_multiplier"
    xml_inputs:
      penalty_rate: "//CCMSRequest/Case/PenaltyRate"
      overdue_days: "//CCMSRequest/Case/OverdueDays"
      risk_multiplier: "//CCMSRequest/Case/RiskFactor"
    xml_output: "//CCMSResponse/ComputedPenalty"

synergy:
  SYN_FORMULA_01:
    name: "risk_weighted_exposure"
    formula: "amount * (1 - haircut / 100) * volatility"
    xml_inputs:
      amount: "//SynergyRequest/Exposure/Amount"
      haircut: "//SynergyRequest/Exposure/Haircut"
      volatility: "//SynergyRequest/Exposure/Volatility"
    xml_output: "//SynergyResponse/WeightedExposure"
"""

FILES["framework/calculators/evaluator.py"] = """from decimal import Decimal, ROUND_HALF_UP

class FormulaEngine:
    @staticmethod
    def calculate_ccms_penalty(rate: str, days: int, multiplier: str) -> Decimal:
        r = Decimal(str(rate))
        d = Decimal(str(days))
        m = Decimal(str(multiplier))
        return (r * d * m).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def calculate_synergy_exposure(amount: str, haircut: str, volatility: str) -> Decimal:
        a = Decimal(str(amount))
        h = Decimal(str(haircut))
        v = Decimal(str(volatility))
        res = a * (Decimal("1") - (h / Decimal("100"))) * v
        return res.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
"""

FILES["tests/conftest.py"] = """import pytest

def pytest_addoption(parser):
    parser.addoption("--env", action="store", default="QA", help="QA, UAT1, or UAT2")

@pytest.fixture(scope="session")
def env_config(request):
    env = request.config.getoption("--env").upper()
    endpoints = {
        "QA": "https://qa.api.internal",
        "UAT1": "https://uat1.api.internal",
        "UAT2": "https://uat2.api.internal"
    }
    return {"name": env, "base_url": endpoints.get(env, endpoints["QA"])}

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        req = getattr(item, "_last_request_xml", "None")
        res = getattr(item, "_last_response_xml", "None")
        report.sections.append(("RCA XML Diagnostics", f"Request:\\n{req}\\nResponse:\\n{res}"))
"""

def generate_repo():
    zip_filename = "rbc_api_agent_ecosystem.zip"
    with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
        for filepath, content in FILES.items():
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content.strip() + "\n")
            zipf.write(filepath)
            print(f"Created: {filepath}")
    print(f"\nSuccessfully generated {len(FILES)} files.")
    print(f"Archive created: {zip_filename}")

if __name__ == "__main__":
    generate_repo()
