"""Data access. Every vendor-facing method takes the vendor_id taken from the
authenticated session and only touches documents under that partition key."""
import copy
import uuid
from datetime import datetime, timedelta, timezone

from . import config
from .security import sha256


def now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryBackend:
    def __init__(self):
        self.data: dict[str, dict[tuple, dict]] = {}

    def upsert(self, c, doc):
        self.data.setdefault(c, {})[(doc["vendor_id"], doc["id"])] = copy.deepcopy(doc)

    def get(self, c, id, pk):
        d = self.data.get(c, {}).get((pk, id))
        return copy.deepcopy(d) if d else None

    def query(self, c, pk=None, id=None):
        return [copy.deepcopy(d) for (p, i), d in self.data.get(c, {}).items()
                if (pk is None or p == pk) and (id is None or i == id)]

    def delete(self, c, id, pk):
        self.data.get(c, {}).pop((pk, id), None)


class CosmosBackend:
    CONTAINERS = ["vendors", "invites", "otps", "responses", "settings", "audit"]

    def __init__(self):
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        client = CosmosClient(config.COSMOS_ENDPOINT, DefaultAzureCredential())
        db = client.get_database_client(config.COSMOS_DATABASE)
        self.c = {n: db.get_container_client(n) for n in self.CONTAINERS}

    def upsert(self, c, doc):
        self.c[c].upsert_item(doc)

    def get(self, c, id, pk):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError
        try:
            return self.c[c].read_item(id, partition_key=pk)
        except CosmosResourceNotFoundError:
            return None

    def query(self, c, pk=None, id=None):
        q, params = "SELECT * FROM c WHERE 1=1", []
        if id is not None:
            q += " AND c.id=@id"; params.append({"name": "@id", "value": id})
        kwargs = {"partition_key": pk} if pk is not None else {"enable_cross_partition_query": True}
        return list(self.c[c].query_items(q, parameters=params, **kwargs))

    def delete(self, c, id, pk):
        self.c[c].delete_item(id, partition_key=pk)


ADMIN_PK = "_admin"


class Repo:
    def __init__(self, backend):
        self.b = backend

    # ---- admin-side (never reachable with a vendor session) ----
    def create_vendor(self, name: str, email: str) -> dict:
        vid = uuid.uuid4().hex
        v = {"id": vid, "vendor_id": vid, "name": name, "email": email.strip().lower()}
        self.b.upsert("vendors", v)
        return v

    def list_vendors(self) -> list[dict]:
        return sorted(self.b.query("vendors"), key=lambda v: v["name"].lower())

    def get_vendor(self, vendor_id: str):
        return self.b.get("vendors", vendor_id, vendor_id)

    def create_invite(self, vendor_id: str, token: str) -> None:
        self.b.upsert("invites", {
            "id": sha256(token), "vendor_id": vendor_id,
            "expires": (now() + timedelta(hours=config.INVITE_TTL_HOURS)).isoformat()})

    def get_questions(self) -> list[dict]:
        d = self.b.get("settings", "questions", ADMIN_PK)
        return d["items"] if d else []

    def set_questions(self, items: list[dict]) -> None:
        self.b.upsert("settings", {"id": "questions", "vendor_id": ADMIN_PK, "items": items})

    # ---- auth flow ----
    def redeem_invite(self, token: str):
        """Returns vendor_id for a valid, unexpired invite token (reusable until expiry)."""
        docs = self.b.query("invites", id=sha256(token))
        if not docs or datetime.fromisoformat(docs[0]["expires"]) < now():
            return None
        return docs[0]["vendor_id"]

    def set_otp(self, vendor_id: str, otp: str) -> None:
        self.b.upsert("otps", {
            "id": "otp", "vendor_id": vendor_id, "hash": sha256(otp + vendor_id), "attempts": 0,
            "expires": (now() + timedelta(minutes=config.OTP_TTL_MINUTES)).isoformat()})

    def check_otp(self, vendor_id: str, otp: str) -> bool:
        d = self.b.get("otps", "otp", vendor_id)
        if not d or datetime.fromisoformat(d["expires"]) < now() or d["attempts"] >= config.MAX_OTP_ATTEMPTS:
            return False
        d["attempts"] += 1
        ok = d["hash"] == sha256(otp.strip() + vendor_id)
        if ok:
            self.b.delete("otps", "otp", vendor_id)
        else:
            self.b.upsert("otps", d)
        return ok

    # ---- vendor-scoped ----
    def get_response(self, vendor_id: str) -> dict:
        return self.b.get("responses", "response", vendor_id) or {
            "id": "response", "vendor_id": vendor_id, "answers": {}, "status": "draft"}

    def save_response(self, vendor_id: str, answers: dict, submit: bool) -> dict:
        r = self.get_response(vendor_id)
        if r["status"] == "submitted":
            return r
        valid = {q["id"] for q in self.get_questions()}
        r["answers"] = {k: str(v)[:5000] for k, v in answers.items() if k in valid}
        if submit:
            r["status"] = "submitted"
            r["submitted_at"] = now().isoformat()
        self.b.upsert("responses", r)
        self.audit(vendor_id, "submit" if submit else "save")
        return r

    def audit(self, vendor_id: str, event: str, detail: str = "") -> None:
        self.b.upsert("audit", {"id": uuid.uuid4().hex, "vendor_id": vendor_id,
                                "event": event, "detail": detail, "at": now().isoformat()})

    def get_audit(self, vendor_id: str) -> list[dict]:
        return sorted(self.b.query("audit", pk=vendor_id), key=lambda a: a["at"], reverse=True)


def make_repo() -> Repo:
    return Repo(CosmosBackend() if config.COSMOS_ENDPOINT else MemoryBackend())
