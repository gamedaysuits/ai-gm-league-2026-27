"""A small Shopify Admin GraphQL client for the coverage publisher: upload images, create/update the week's blog
article (by handle, so a re-run updates instead of duplicating), and refresh the tracker page.

Auth, from the environment (GitHub repo secrets on the weekly runner):
  SHOPIFY_STORE          xd7yhc-zg.myshopify.com
  SHOPIFY_CLIENT_ID      } a Dev Dashboard app installed on the store (client credentials grant, 24 h tokens), or
  SHOPIFY_CLIENT_SECRET  }
  SHOPIFY_ADMIN_TOKEN    a static Admin API token instead
Scopes: write_content (blog articles + pages), write_files.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

API_VERSION = "2026-07"


class ShopifyError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("SHOPIFY_STORE") and (os.environ.get("SHOPIFY_ADMIN_TOKEN") or (
        os.environ.get("SHOPIFY_CLIENT_ID") and os.environ.get("SHOPIFY_CLIENT_SECRET"))))


class Shopify:
    def __init__(self) -> None:
        self.store = os.environ["SHOPIFY_STORE"].removeprefix("https://").strip("/")
        token = os.environ.get("SHOPIFY_ADMIN_TOKEN")
        if not token:
            r = httpx.post(f"https://{self.store}/admin/oauth/access_token", timeout=30, data={
                "grant_type": "client_credentials", "client_id": os.environ["SHOPIFY_CLIENT_ID"],
                "client_secret": os.environ["SHOPIFY_CLIENT_SECRET"]})
            if r.status_code != 200:
                raise ShopifyError(f"token request failed: HTTP {r.status_code} {r.text[:300]}")
            token = r.json()["access_token"]
        self.http = httpx.Client(base_url=f"https://{self.store}/admin/api/{API_VERSION}", timeout=60,
                                 headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"})

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(4):
            r = self.http.post("/graphql.json", json={"query": query, "variables": variables or {}})
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            data = r.json()
            if r.status_code >= 400 or data.get("errors"):
                if any("THROTTLED" in str(x) for x in data.get("errors") or []):
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ShopifyError(f"HTTP {r.status_code}: {data.get('errors') or r.text[:300]}")
            return data["data"]
        raise ShopifyError("Shopify kept throttling")

    @staticmethod
    def _user_errors(payload: dict[str, Any]) -> None:
        errs = payload.get("userErrors") or []
        if errs:
            raise ShopifyError("; ".join(f"{e.get('field')}: {e.get('message')}" for e in errs))

    # ------------------------------------------------------------------ files

    def upload_image(self, path: Path, alt: str) -> str:
        """Upload a PNG and return its CDN URL once Shopify has processed it."""
        staged = self.gql("""mutation S($input: [StagedUploadInput!]!) { stagedUploadsCreate(input: $input) {
              stagedTargets { url resourceUrl parameters { name value } } userErrors { field message } } }""",
                          {"input": [{"resource": "IMAGE", "filename": path.name, "mimeType": "image/png",
                                      "httpMethod": "POST", "fileSize": str(path.stat().st_size)}]})["stagedUploadsCreate"]
        self._user_errors(staged)
        target = staged["stagedTargets"][0]
        r = httpx.post(target["url"], data={p["name"]: p["value"] for p in target["parameters"]},
                       files={"file": (path.name, path.read_bytes(), "image/png")}, timeout=120)
        if r.status_code >= 300:
            raise ShopifyError(f"staged upload failed: HTTP {r.status_code} {r.text[:200]}")
        made = self.gql("""mutation F($files: [FileCreateInput!]!) { fileCreate(files: $files) {
              files { id fileStatus } userErrors { field message } } }""",
                        {"files": [{"originalSource": target["resourceUrl"], "contentType": "IMAGE", "alt": alt[:512],
                                    "filename": path.name}]})["fileCreate"]
        self._user_errors(made)
        fid = made["files"][0]["id"]
        for _ in range(60):
            node = self.gql("""query N($id: ID!) { node(id: $id) { ... on MediaImage { fileStatus image { url } }
                               ... on GenericFile { fileStatus url } } }""", {"id": fid})["node"] or {}
            if node.get("fileStatus") == "READY":
                url = (node.get("image") or {}).get("url") or node.get("url")
                if url:
                    return url
            if node.get("fileStatus") == "FAILED":
                raise ShopifyError(f"Shopify could not process {path.name}")
            time.sleep(2)
        raise ShopifyError(f"timed out waiting for {path.name}")

    # ------------------------------------------------------------------ blog

    def blog_id(self, handle: str = "news") -> str:
        nodes = self.gql("""query B($q: String!) { blogs(first: 5, query: $q) { nodes { id handle } } }""",
                         {"q": f"handle:{handle}"})["blogs"]["nodes"]
        for n in nodes:
            if n["handle"] == handle:
                return n["id"]
        raise ShopifyError(f"no blog with handle {handle}")

    def article_by_handle(self, handle: str) -> dict[str, Any] | None:
        nodes = self.gql("""query A($q: String!) { articles(first: 5, query: $q) { nodes { id handle } } }""",
                         {"q": f"handle:{handle}"})["articles"]["nodes"]
        return next((n for n in nodes if n["handle"] == handle), None)

    def upsert_article(self, *, blog_id: str, handle: str, title: str, body: str, summary: str, tags: list[str],
                       image_url: str | None, image_alt: str, seo_title: str, seo_description: str) -> dict[str, Any]:
        fields: dict[str, Any] = {"title": title, "body": body, "summary": summary, "tags": tags, "isPublished": True,
                                  "metafields": [
                                      {"namespace": "global", "key": "title_tag", "type": "single_line_text_field", "value": seo_title},
                                      {"namespace": "global", "key": "description_tag", "type": "single_line_text_field",
                                       "value": seo_description}]}
        if image_url:
            fields["image"] = {"url": image_url, "altText": image_alt[:512]}
        existing = self.article_by_handle(handle)
        if existing:
            out = self.gql("""mutation U($id: ID!, $a: ArticleUpdateInput!) { articleUpdate(id: $id, article: $a) {
                  article { id handle } userErrors { field message } } }""", {"id": existing["id"], "a": fields})["articleUpdate"]
        else:
            out = self.gql("""mutation C($a: ArticleCreateInput!) { articleCreate(article: $a) {
                  article { id handle } userErrors { field message } } }""",
                           {"a": {**fields, "blogId": blog_id, "handle": handle, "author": {"name": "Game Day Suits"}}})["articleCreate"]
        self._user_errors(out)
        return out["article"]

    # ------------------------------------------------------------------ pages

    def update_page(self, handle: str, body: str, *, seo_title: str | None = None, seo_description: str | None = None) -> str:
        nodes = self.gql("""query P($q: String!) { pages(first: 5, query: $q) { nodes { id handle } } }""",
                         {"q": f"handle:{handle}"})["pages"]["nodes"]
        page = next((n for n in nodes if n["handle"] == handle), None)
        if not page:
            raise ShopifyError(f"no page with handle {handle}")
        fields: dict[str, Any] = {"body": body}
        meta = [(k, v) for k, v in (("title_tag", seo_title), ("description_tag", seo_description)) if v]
        if meta:
            fields["metafields"] = [{"namespace": "global", "key": k, "type": "single_line_text_field", "value": v} for k, v in meta]
        out = self.gql("""mutation U($id: ID!, $p: PageUpdateInput!) { pageUpdate(id: $id, page: $p) {
              page { id } userErrors { field message } } }""", {"id": page["id"], "p": fields})["pageUpdate"]
        self._user_errors(out)
        return page["id"]
