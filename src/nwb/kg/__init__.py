"""Graph-backed knowledge and memory, built on FalkorDB GraphRAG-SDK (company knowledge) and Graphiti (per-user
temporal memory). Company load writes one graph per access level from ``config/graph.yaml``; the tag map
lives in ``config/classification.yaml``. This package wires those libraries to our config, models and access rules."""
