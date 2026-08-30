---
name: oap-profile-authoring
description: Generate, validate, review, and safely activate portable Open Agent Profile 1.0 specialists.
license: MIT
compatibility: Requires Merced AI 0.4 or an OAP 1.0 compatible harness.
metadata:
  standard: OAP 1.0
---

# OAP profile authoring

Use `merced-ai profile generate PROMPT` or the Profiles page when a user asks for a reusable
specialist. The generation harness is run with tools and consequential permissions denied; Merced
AI compiles its small draft into canonical OAP and validates the result before persistence.

Native MagAgent and Loro sessions may use their governed profile-creation tools. An agent that
independently identifies a useful subagent profile must create a proposal, not silently activate
new authority. Never invent tools, skills, MCP servers, credentials, commands, paths, or learned
state. `~/.agentprofiles` is the portable user location; a project-local `.agents` profile is the
default.
