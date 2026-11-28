# Source baseline and verification notes

This package is an update built from the Kimi-DLP source snapshots available in the
working Library during this task. It is not a direct clone/export of the entire
GitHub repository.

- `kimi/search/youtube_search.py` is byte-identical to the supplied
  `youtube_search(4).py` snapshot (SHA-256:
  `4bf5c1660fe0bc34f3f2bc957a3c7b9368a7de092bd1aefad31b6db852dc5b2a`).
- The YouTube extractor changes are based on the supplied `youtube.py` snapshot.
  The inspected GitHub `main` tree advertised a different, larger `youtube.py`
  (62,877 bytes versus 50,592 bytes in the supplied snapshot). The newer remote
  file was not silently substituted because its complete contents were not
  available as a local source archive for a safe patch-and-test cycle.
- The Instagram extractor is based on the supplied `instagram.py` snapshot.
- The Cython accelerator source is the supplied `_youtube_fast.pyx` companion
  for the supplied YouTube extractor.

Before replacing a newer local checkout or deploying to production, compare this
package with that checkout and run its integration tests. The automated tests in
`tests/` are offline/mock-based; they do not guarantee that live Instagram or
YouTube endpoints will accept a request, account, cookie, region, or client profile.
