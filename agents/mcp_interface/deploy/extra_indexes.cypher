// Extra MCP-server indexes, created while the temporary dump database is writable.
// Relationship-id indexes are emitted by prepare_dump.sh: Neo4j range indexes are
// per relationship type and Cypher cannot execute dynamic DDL without APOC.
CREATE FULLTEXT INDEX kgbcia_node_fulltext IF NOT EXISTS
FOR (node:`biolink:NamedThing`)
ON EACH [node.name, node.synonym, node.equivalent_identifiers];

CALL db.awaitIndexes();
