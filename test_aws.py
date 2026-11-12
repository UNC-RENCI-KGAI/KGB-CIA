from __future__ import annotations

import os
import secrets

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError

uri = os.environ["NEO4J_URI"]
auth = (os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
probe_user = "kgbcia_aws_probe"
probe_password = secrets.token_urlsafe(24)

driver = GraphDatabase.driver(uri, auth=auth)
try:
	driver.verify_connectivity()

	with driver.session(database="neo4j") as session:
		answer = session.run("RETURN 1 AS connected").single()
		assert answer["connected"] == 1
		print("Bolt login: OK")

		try:
			session.execute_write(
			   lambda tx: tx.run(
			       "CREATE (:`biolink:NamedThing` {id: 'kgbcia:aws-write-probe'})"
			   ).consume()
			)
		except Neo4jError as error:
			print(f"Graph write rejected as expected: {error.code}")
		else:
			raise SystemExit("ERROR: graph CREATE unexpectedly succeeded")

	created = False
	try:
		   with driver.session(database="system") as session:
		       session.execute_write(
			   lambda tx: tx.run(
			       f"CREATE USER {probe_user} IF NOT EXISTS "
			       "SET PASSWORD $password CHANGE NOT REQUIRED",
			       password=probe_password,
			   ).consume()
		       )
		       created = True
		       print("System-user creation by kgbcia: ALLOWED (Community security risk confirmed)")
	finally:
		if created:
			with driver.session(database="system") as session:
				session.execute_write(
				   lambda tx: tx.run(f"DROP USER {probe_user} IF EXISTS").consume()
				)
			print("Temporary system user removed: OK")
finally:
	driver.close()
