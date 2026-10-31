"""Portal Harness - Redmine Ticketing Automation Agent.

A permission-first automation harness for Redmine:

* daily overview e-mails
* dashboards (overview, SLA, team, performance, time spent) and CSV reports
* "assigned to me" ticket lists
* closure templates
* ticket closure with mandatory human review and approval
* Redmine REST API connectivity

The core rule of this package: **nothing is changed in Redmine without an
explicit, recorded human approval** (see ``redmine_agent.guard``).
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
