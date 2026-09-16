# Third-Party Notices

Ka-ching is licensed under AGPL-3.0. The following third-party components keep
their own licenses; the project license does not replace them.

## Bundled fonts

The repository redistributes these font binaries under the SIL Open Font License
1.1. Their canonical copyright notices and license texts are included beside the
font files:

| Font family | License file |
|---|---|
| DM Mono | `assets/fonts/DM-Mono-OFL.txt` |
| Fraunces | `assets/fonts/OFL.txt` |
| Hanken Grotesk | `assets/fonts/Hanken-Grotesk-OFL.txt` |
| Newsreader | `assets/fonts/Newsreader-OFL.txt` |

## Python packages

Python packages are installed from their publishers and are not vendored in this
repository. Direct dependencies currently include:

| Package | License |
|---|---|
| PyMuPDF | AGPL-3.0 or an Artifex commercial license; Ka-ching uses the AGPL option |
| pypdf | BSD-3-Clause |
| ofxparse | MIT |
| pytest (development) | MIT |
| Selenium (development) | Apache-2.0 |

Transitive packages retain the licenses supplied in their installed package
metadata. Optional Poppler tools are installed separately by the operating-system
package manager and retain their upstream license.
