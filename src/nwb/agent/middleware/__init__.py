"""Our harness middleware (LangChain ``AgentMiddleware``). Small, single-purpose, state-based:

- ``RuntimeContextMiddleware``: the Memorae ``<runtime>`` block (temporal, account, access, guessed memories). No
  history: the checkpointer owns history and the summariser owns the budget.
- ``BootstrapMiddleware``: one Hermes onboarding beat per chat turn. Heartbeat and cron never get one.
- ``ToolAuditMiddleware``: every tool call -> ``tool_calls`` receipt + data-flow hop (status/artifact, no parsing).
- ``ApprovedCallMiddleware``: a human-approved skill tool runs with the reviewer's edited args.
The memory turn counter is not middleware: ``nwb.jobs.consolidate`` starts the memory-keeper after N turns.
"""
