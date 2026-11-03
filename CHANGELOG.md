# Changelog

## 1.1.1

- A cast to a closed app opens Foyer only when Foyer is still the app installed under the paired id. If another app took its place (a side-loaded Foyer replaced in the developer slot), nothing is opened and `tv_wake` shows `not_installed`; pair the TV again.

## 1.1.0

- Foyer Renderer installs as a Home Assistant app (Settings > Apps > App store > Repositories, add this repository). It makes its own secret when none is set.
- Home Assistant offers a running Foyer Renderer app to every paired TV, so no renderer address or secret is typed.
- Renderer 0.5.0: base images pinned to 3.24.

## 1.0.1

- Download diagnostics for each paired TV (Settings > Devices & services > Foyer). The file leaves out pairing keys, the renderer secret, network addresses, titles and entity names.
- Issue forms for problem reports and feature requests.

## 1.0.0

- First release.
