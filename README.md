# Starling's mod builds

Native mods are compiled into [Starling](https://github.com/starling-client/starling), and a phone can't do that itself. When a player taps **Use GitHub (slower)**, Starling's gateway asks this repository to compile the game with their mods, and the phone downloads the result from the gateway.

- `build.yml` compiles one player's mods. A mod's code runs while it compiles, so that happens in a sandbox with no network, and the job's token can only read this repository.
- `base.yml` makes the clean build cache for one version of the game. It only runs trusted code, and it's the only job that writes the cache.
- `versions.json` says which commit each version of the game is.

Runs and their logs are public. Results are kept for a day.

More in [docs/starling-native-mods.md](https://github.com/starling-client/starling/blob/starling/docs/starling-native-mods.md).
