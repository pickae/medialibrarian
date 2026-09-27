# `find-fragment-candidates`

Reports the recurring fragments a library's names still carry — the release tags,
site names and encoder marks the individual cleaning pass would strip if it knew
about them — so they can be reviewed and added to `data/fragments.txt`. It reads
nothing but names and writes nothing but its report: no file is touched.

Point it at a folder, and a tree of that folder is generated and parsed; point it
at a tree file that already exists, and that is parsed directly. The report lands
beside whichever it was given, as `fragmentCandidates.txt`, or wherever `-o`
says.

`-m` is the prevalence floor: how many distinct names have to carry a candidate
before it is worth reporting. The default of 2 hides one-off title words, which
is most of what a first run would otherwise show; `-m 1` lists every candidate.
