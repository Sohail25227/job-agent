"""Skill taxonomy and extraction.

Deliberately a curated alias table rather than an NLP model: it needs no
downloads, runs instantly, and is trivially auditable when a score looks wrong.
Add aliases as you meet new phrasings in real JDs.
"""

from __future__ import annotations

import re
from functools import lru_cache

# canonical name -> surface forms seen in job descriptions
SKILL_ALIASES: dict[str, list[str]] = {
    # Languages
    "java": ["java", "core java", "java 8", "java 11", "java 17", "java 21", "j2ee", "jee"],
    "kotlin": ["kotlin"],
    "scala": ["scala"],
    "python": ["python", "python3"],
    "go": ["golang", "go lang"],
    "node.js": ["node.js", "nodejs", "node js"],
    "typescript": ["typescript"],
    "c++": ["c++", "cpp"],
    "c#": ["c#", ".net", "dotnet", "asp.net"],
    "sql": ["sql", "pl/sql", "t-sql", "n1ql"],
    "bash": ["bash", "shell script", "shell scripting"],
    # Frameworks
    "spring": ["spring", "spring framework", "spring mvc"],
    "spring boot": ["spring boot", "springboot"],
    "spring cloud": ["spring cloud"],
    "hibernate": ["hibernate", "jpa", "spring data jpa"],
    "quarkus": ["quarkus"],
    "micronaut": ["micronaut"],
    "django": ["django"],
    "fastapi": ["fastapi"],
    "flask": ["flask"],
    "express": ["express.js", "expressjs"],
    "react": ["react", "react.js", "reactjs", "react 19", "next.js"],
    "ai/llm": ["llm", "llms", "generative ai", "genai", "gen ai", "spring ai", "langchain",
               "rag", "openai", "llama", "prompt engineering", "vector database", "embeddings"],
    # Architecture / practices
    "microservices": ["microservice", "microservices", "micro-services", "micro services"],
    "rest api": ["rest", "restful", "rest api", "restful api", "rest apis", "web services",
                 "swagger", "openapi"],
    "graphql": ["graphql"],
    "grpc": ["grpc", "protobuf", "protocol buffers"],
    "soap": ["soap", "wsdl"],
    "event driven": ["event driven", "event-driven", "eda", "pub/sub", "publish subscribe"],
    "domain driven design": ["ddd", "domain driven design", "domain-driven"],
    "distributed systems": ["distributed systems", "distributed system"],
    "system design": ["system design", "high level design", "low level design"],
    "tdd": ["tdd", "test driven", "test-driven"],
    "agile": ["agile", "scrum", "kanban", "sprint"],
    "ci/cd": ["ci/cd", "cicd", "continuous integration", "continuous delivery", "continuous deployment"],
    "code review": ["code review", "peer review"],
    "design patterns": ["design pattern", "design patterns", "solid principles"],
    # Messaging
    "kafka": ["kafka", "apache kafka", "confluent", "ksql", "kafka streams"],
    "rabbitmq": ["rabbitmq", "rabbit mq", "amqp"],
    "activemq": ["activemq", "jms"],
    "pulsar": ["pulsar"],
    "sqs": ["sqs", "sns", "eventbridge"],
    # Data stores
    "postgresql": ["postgres", "postgresql"],
    "mysql": ["mysql", "mariadb"],
    "oracle": ["oracle db", "oracle database", "oracle 19c", "oracle sql"],
    "sql server": ["sql server", "mssql"],
    "couchbase": ["couchbase"],
    "mongodb": ["mongodb", "mongo db", "mongo"],
    "cassandra": ["cassandra", "scylla"],
    "redis": ["redis", "elasticache"],
    "elasticsearch": ["elasticsearch", "elastic search", "opensearch", "elk"],
    "dynamodb": ["dynamodb"],
    "snowflake": ["snowflake"],
    # Platform / infra
    "docker": ["docker", "containerization", "containerisation", "podman"],
    "kubernetes": ["kubernetes", "k8s", "openshift", "eks", "aks", "gke"],
    "helm": ["helm", "helm chart", "helm charts"],
    "terraform": ["terraform", "iac", "infrastructure as code"],
    "jenkins": ["jenkins"],
    "gitlab ci": ["gitlab ci", "gitlab-ci"],
    "github actions": ["github actions"],
    "argocd": ["argocd", "argo cd", "gitops"],
    "maven": ["maven"],
    "gradle": ["gradle"],
    "git": ["git", "github", "bitbucket", "version control"],
    "linux": ["linux", "unix"],
    "aws": ["aws", "amazon web services", "ec2", "lambda", "s3"],
    "azure": ["azure"],
    "gcp": ["gcp", "google cloud"],
    "observability": ["observability", "prometheus", "grafana", "datadog", "splunk",
                      "distributed tracing", "opentelemetry", "kibana"],
    "performance tuning": ["performance tuning", "performance optimization", "latency optimization",
                           "jvm tuning", "profiling"],
    "security": ["oauth", "oauth2", "jwt", "spring security", "sso", "saml", "rbac"],
    # Testing
    "junit": ["junit", "test ng", "testng"],
    "mockito": ["mockito", "mocking"],
    "cucumber": ["cucumber", "bdd"],
    "postman": ["postman", "rest assured", "restassured"],
    # Adjacent
    "kafka connect": ["kafka connect", "debezium", "cdc", "change data capture"],
    "batch processing": ["spring batch", "batch job", "batch processing"],
    "caching": ["caching", "cache", "hazelcast", "ehcache"],
    "telecom domain": ["bss", "oss", "amdocs", "telecom", "catalog", "order management",
                       "billing", "charging", "crm"],
}

SENIORITY_TERMS: dict[str, int] = {
    "intern": 0,
    "graduate": 0,
    "junior": 1,
    "associate": 2,
    "": 3,
    "mid": 3,
    "senior": 5,
    "sr": 5,
    "lead": 8,
    "staff": 9,
    "principal": 11,
    "architect": 11,
}


@lru_cache(maxsize=1)
def _compiled() -> list[tuple[str, re.Pattern[str]]]:
    compiled = []
    for canonical, aliases in SKILL_ALIASES.items():
        # Longest-first so "spring boot" wins over "spring" inside the same alt.
        alts = sorted({re.escape(a) for a in aliases}, key=len, reverse=True)
        pattern = re.compile(r"(?<![\w+#.])(?:" + "|".join(alts) + r")(?![\w+#])", re.I)
        compiled.append((canonical, pattern))
    return compiled


def extract_skills(text: str | None) -> set[str]:
    """Return the canonical skills mentioned anywhere in the text."""
    if not text:
        return set()
    found = set()
    for canonical, pattern in _compiled():
        if pattern.search(text):
            found.add(canonical)
    # "spring boot" implies "spring"; keep the graph honest for scoring.
    if "spring boot" in found:
        found.add("spring")
    return found


def derive_skills(resume: dict | None) -> dict[str, float]:
    """Turn a structured resume into the weighted skills the scorer wants.

    Where a skill appears is the only honest signal available without asking the
    user to rate themselves: a skills section is a claim, whereas the same skill
    inside a job bullet is evidence they were paid to use it. Both count, the
    latter counts for more.
    """
    if not resume:
        return {}

    listed: set[str] = set()
    for items in (resume.get("skills") or {}).values():
        listed |= extract_skills(" , ".join(str(i) for i in items or []))

    used: set[str] = set()
    for role in resume.get("experience") or []:
        text = " ".join(
            [str(role.get("title") or "")] + [str(b) for b in role.get("bullets") or []]
        )
        used |= extract_skills(text)
    for project in resume.get("projects") or []:
        text = " ".join(
            [str(project.get("name") or "")] + [str(b) for b in project.get("bullets") or []]
        )
        used |= extract_skills(text)

    weights: dict[str, float] = {}
    for skill in listed | used:
        if skill in used:
            weights[skill] = 1.0 if skill in listed else 0.9
        else:
            weights[skill] = 0.6
    return dict(sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])))


EXPERIENCE_PATTERNS = [
    re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*[-–to]{1,3}\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)", re.I),
    re.compile(r"(?:minimum|min\.?|at least|atleast)\s*(?:of\s*)?(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years|yrs|year)", re.I),
    re.compile(r"(\d{1,2})\s*(?:years|yrs)\s*(?:of\s*)?(?:relevant\s*|professional\s*|hands-on\s*)?experience", re.I),
]


def parse_required_experience(text: str | None) -> tuple[int | None, int | None]:
    """Best-effort (min_years, max_years) required by a JD."""
    if not text:
        return None, None
    for pattern in EXPERIENCE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = [int(g) for g in match.groups() if g is not None]
        if not groups:
            continue
        low = min(groups)
        high = max(groups) if len(groups) > 1 else None
        if 0 <= low <= 30:
            return low, high
    return None, None


def seniority_from_title(title: str) -> int:
    """Rough years-of-experience implied by a job title."""
    lowered = f" {title.lower()} "
    best = 3
    for term, years in SENIORITY_TERMS.items():
        if term and re.search(rf"\b{re.escape(term)}\b", lowered):
            best = max(best, years) if years > 3 else years
    return best
