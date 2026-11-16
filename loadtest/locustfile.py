"""Load test for the portal: applicants sign in, start an application, upload T4s and check on them.

Against the local stack (`docker compose up`, or the portal and worker started by hand):

    locust -f loadtest/locustfile.py --host http://localhost:8000 --headless -u 25 -r 5 -t 1m --csv results/locust

Each simulated applicant signs in on the local sign-in page as "Load Test n", so every one is a separate
person with their own limits. Azure's sign-in can't be scripted this way, which is the point of it; in Azure
the document pipeline behind the portal is load tested with `python -m loanlens.burst` instead.
"""

from __future__ import annotations

import itertools
import random
import re

from locust import HttpUser, between, task

from loanlens import samples

_rng = random.Random(11)
SLIPS = []
for _ in range(8):
    fields = samples.invent(_rng)
    SLIPS.append((fields, samples.draw(fields, _rng)[0]))
PEOPLE = itertools.count(1)
TOKEN = re.compile(r'name="csrf" value="([^"]+)"')


class Applicant(HttpUser):
    wait_time = between(1, 3)

    def token(self, path: str, name: str) -> str:
        return TOKEN.search(self.client.get(path, name=name).text).group(1)

    def on_start(self) -> None:
        self.n = next(PEOPLE)
        self.client.post(
            "/dev/sign-in", data={"person": f"dev-load-{self.n}", "csrf": self.token("/dev/sign-in", "/dev/sign-in")}
        )
        start = {"loan_type": "car", "amount": "25000", "csrf": self.token("/", "/")}
        created = self.client.post("/applications", data=start, name="/applications", allow_redirects=False)
        self.path = created.headers["location"]
        self.uploads = 0

    @task(3)
    def check_on_it(self) -> None:
        self.client.get(self.path, name="/applications/[id]")

    @task(1)
    def upload_a_t4(self) -> None:
        if self.uploads >= 10:
            return
        fields, pdf = random.choice(SLIPS)
        slip = samples.with_truth(pdf, {**fields, "employee_name": f"Load Test {self.n}"})
        token = self.token(self.path, "/applications/[id]")
        self.client.post(
            f"{self.path}/documents",
            data={"kind": "t4", "csrf": token},
            files={"file": ("t4.pdf", slip, "application/pdf")},
            name="/applications/[id]/documents",
            allow_redirects=False,
        )
        self.uploads += 1
