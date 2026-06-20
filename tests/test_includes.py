"""Nested includes: ``?include=`` hydrates relation guids into full objects.

HTTP-level (through the real route → resolver path) on a user/post/comment graph.
Without ``include`` a relation stays a guid / list of guids (unchanged); with it,
the value becomes the full neighbor object(s), nested. Reads only.
"""

import unittest

from harness import Base


class TestIncludes(Base):
    def setUp(self):
        super().setUp()
        self.put_type("user", fields={"name": "string"})
        self.put_type("post", fields={"title": "string"}, relations={
            "author": {"to": "user", "cardinality": "many_to_one", "inverse": "posts"}})
        self.put_type("comment", fields={"text": "string"}, relations={
            "on": {"to": "post", "cardinality": "many_to_one", "inverse": "comments"},
            "by": {"to": "user", "cardinality": "many_to_one", "inverse": "comments_made"}})
        self.ann = self.create("user", {"name": "ann"})["_guid"]
        self.bob = self.create("user", {"name": "bob"})["_guid"]
        self.pid = self.create("post", {"title": "hello", "author": self.ann})["_guid"]
        self.c1 = self.create("comment", {"text": "nice", "on": self.pid, "by": self.bob})["_guid"]
        self.c2 = self.create("comment", {"text": "ty", "on": self.pid, "by": self.ann})["_guid"]

    def g(self, path):
        st, b, _ = self.get(path)
        self.assertEqual(st, 200, b)
        return b

    # --- hydration --------------------------------------------------------
    def test_to_one_hydrated(self):
        p = self.g(f"/objects/post/{self.pid}?include=author")
        self.assertEqual(p["author"]["name"], "ann")
        self.assertEqual(p["author"]["_type"], "user")

    def test_without_include_stays_guid(self):
        p = self.g(f"/objects/post/{self.pid}")
        self.assertEqual(p["author"], self.ann)

    def test_to_many_inverse_hydrated(self):
        u = self.g(f"/objects/user/{self.ann}?include=posts")
        self.assertEqual([x["title"] for x in u["posts"]], ["hello"])

    def test_to_many_empty_stays_list(self):
        u = self.g(f"/objects/user/{self.bob}?include=posts")
        self.assertEqual(u["posts"], [])

    def test_nested_two_levels(self):
        p = self.g(f"/objects/post/{self.pid}?include=comments.by")
        self.assertEqual(sorted(c["text"] for c in p["comments"]), ["nice", "ty"])
        self.assertEqual(sorted(c["by"]["name"] for c in p["comments"]), ["ann", "bob"])

    def test_three_levels(self):
        c = self.g(f"/objects/comment/{self.c1}?include=on.author")
        self.assertEqual(c["on"]["title"], "hello")
        self.assertEqual(c["on"]["author"]["name"], "ann")

    def test_multiple_paths(self):
        p = self.g(f"/objects/post/{self.pid}?include=author,comments")
        self.assertEqual(p["author"]["name"], "ann")
        self.assertEqual(len(p["comments"]), 2)

    def test_list_include(self):
        b = self.g("/objects/post?include=author")
        self.assertEqual(b["objects"][0]["author"]["name"], "ann")

    def test_guid_only_endpoint(self):
        p = self.g(f"/object/{self.pid}?include=author")
        self.assertEqual(p["author"]["name"], "ann")

    def test_shared_neighbor_hydrated_for_all(self):
        self.create("post", {"title": "world", "author": self.ann})
        b = self.g("/objects/post?include=author")
        self.assertEqual(sorted(o["author"]["name"] for o in b["objects"]), ["ann", "ann"])

    def test_unlinked_to_one_is_null(self):
        p2 = self.create("post", {"title": "orphan"})["_guid"]
        p = self.g(f"/objects/post/{p2}?include=author")
        self.assertIsNone(p["author"])

    # --- errors -----------------------------------------------------------
    def test_unknown_relation_errors(self):
        st, b, _ = self.get(f"/objects/post/{self.pid}?include=nope")
        self.assertEqual(st, 400, b)

    def test_field_not_relation_errors(self):
        st, b, _ = self.get(f"/objects/post/{self.pid}?include=title")
        self.assertEqual(st, 400, b)

    def test_too_deep_errors(self):
        st, b, _ = self.get(f"/objects/post/{self.pid}?include=a.b.c.d.e")
        self.assertEqual(st, 400, b)


if __name__ == "__main__":
    unittest.main()
