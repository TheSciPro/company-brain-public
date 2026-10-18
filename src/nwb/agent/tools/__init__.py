"""Plain LangChain ``@tool`` functions. Identity comes from ``runtime: ToolRuntime[RuntimeContext]``, never from
arguments; descriptions are the docstrings; domain errors become ``ToolException`` so the model sees them as
``status="error"`` tool messages. Each module exposes one small class whose ``tools()`` returns its tools
(constructor injection of the services they call)."""
