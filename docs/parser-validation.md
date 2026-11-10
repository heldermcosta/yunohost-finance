# ActivoBank parser validation (real statements, counts only)

Run on the statement folders with the parser in this repository. Only counts and the names
of statements that need review are recorded here; no amounts or descriptions.

| Folder | Statements parsed | ok | warning | failed | Transactions |
|---|---|---|---|---|---|
| Helder Costa | 112 | 111 | 1 | 0 | 3,556 |
| Conjunta Elsa | 104 | 104 | 0 | 0 | 1,135 |

Not statements (no period line): the two annual commission notices, and one 0-byte file in
Elsa's folder (`EXTRATO COMBINADO 2020008.pdf`, already known to be corrupt).

Needs review: `EXTRATO COMBINADO 2018008.pdf` (Helder). Totals agree, but 17 printed running
balances do not follow. The statement has a page break with a second column header.

Differences from the previous importer: it had 110 Helder statements and 3,525 transactions.
The two extra statements are the newest months (2026-009 and 2026-010), which were added after
that importer last ran. Elsa's extra two statements explain the difference the same way.
