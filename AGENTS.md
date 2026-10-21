# Hinweise für Agenten und Mitwirkende

Dieses Repository merged Änderungen **automatisch nach `main`** – ohne Pull
Request. Bitte halte dich daran:

## Keine Pull Requests

* **Nie** einen Pull Request erstellen, öffnen oder mergen
  (`gh pr create`, `gh pr merge`, "Create pull request"-Buttons, PR-Vorschläge).
* Kein `pull_request`-Workflow als Ersatz für den Merge benutzen.
* Änderungen landen über den Push auf den Arbeitsbranch in `main`:
  [`.github/workflows/auto-merge.yml`](.github/workflows/auto-merge.yml)
  merged jeden Push auf einen Branch (außer `main` und `renders`) automatisch
  nach `main`.

## Arbeitsweise

```bash
git checkout -b <arbeitsbranch>      # z. B. arena/<session-id>-robloxavatarrenderproject
# ... Änderungen ...
git commit -am "Beschreibung"
git push origin HEAD                 # Auto-Merge nach main startet automatisch
```

* Immer nur den eigenen Arbeitsbranch pushen – **nie direkt auf `main`**.
* Kein Branch-Wechsel auf `main` und keine Commits dort.
* `main` und `renders` sind als Quelle des Auto-Merges ausgeschlossen.
* Schlägt der Auto-Merge fehl (Konflikt, Branch-Schutz), erstellt der Workflow
  ein Issue. **Nicht** als Workaround einen Pull Request anlegen, sondern den
  Konflikt auf dem Arbeitsbranch lösen und erneut pushen.

## Wenn der Push auf `main` abgelehnt wird

Dann fehlen Rechte oder `main` ist geschützt. Lösung (einmalig, durch die
Repository-Verwaltung): Secret `AUTO_MERGE_TOKEN` hinterlegen (Fine-grained
PAT, *Contents: Read and write*, bei Branch-Schutz als Bypass eintragen).
Details: [ANLEITUNG.md](ANLEITUNG.md#auto-merge-jeder-arbeitsbranch-landet-automatisch-in-main).

## Tests

`python -m pytest tools/tests -q` (bzw. der Workflow *Tests*, der bei jedem
Push läuft) muss grün sein. Die Workflow-Dateien werden in
`tools/tests/test_pipeline.py` auf gültiges YAML und die Auto-Merge-Regeln
geprüft.
