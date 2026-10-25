"""``himari-runner`` (V3-P74): the machine Environment's commands and files, as the user.

A process of its own, run by systemd as the user (``coder``) while HIMARI
runs as its service user. HIMARI reaches it through :mod:`himari.runner.api`
only; the runner uses the sandbox's machine profile (``himari.sandbox``)
and nothing else of HIMARI.
"""
