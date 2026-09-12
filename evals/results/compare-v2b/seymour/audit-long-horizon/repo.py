"""In-memory repository with JSON persistence."""

import json

from lib.models import Book, Loan, Member
from lib.utils.text import normalise


class Repo:
    def __init__(self):
        self.books = {}
        self.members = {}
        self.loans = []

    # ---- books
    def add_book(self, book):
        self.books[book.isbn] = book

    def find_by_title(self, query):
        """Books whose title contains the query (case- and accent-insensitive)."""
        wanted = normalise(query)
        return [b for b in self.books.values() if wanted in normalise(b.title)]

    def available_copies(self, isbn):
        out = sum(1 for l in self.loans if l.isbn == isbn and not l.returned_on)
        return self.books[isbn].copies - out

    # ---- members
    def add_member(self, member):
        self.members[member.member_id] = member

    # ---- loans
    def open_loans(self, member_id):
        return [l for l in self.loans if l.member_id == member_id and not l.returned_on]

    # ---- persistence
    def save(self, path):
        data = {"books": [b.__dict__ for b in self.books.values()],
                "members": [{"member_id": m.member_id, "name": m.name} for m in self.members.values()],
                "loans": [l.__dict__ for l in self.loans]}
        with open(path, "w") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)

    def load(self, path):
        with open(path) as handle:
            data = json.load(handle)
        for row in data["books"]:
            self.add_book(Book(**row))
        for row in data["members"]:
            self.add_member(Member(**row))
        self.loans = [Loan(**row) for row in data["loans"]]
