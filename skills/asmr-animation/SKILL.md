# ASMR Animation (Cute Cat Service)

Make a cute cat ASMR animation episode in hayang style with automuse-multi.
The repo lives at `~/workspace/automuse-multi` and the CLI runs from its
root with `python cli.py`.

## The formula (from channel hayang, @hayang04)

- One cute cat character runs one relaxing service: a spa, a hair salon,
  a barber shop, a dental clinic, an ear cleaning shop, a lash salon,
  a foot care shop, a fish-shaped bun stand, or an ice cream truck.
- Each scene is one gentle step of the service, and each step carries
  exactly one soft satisfying sound: brushing fur, pouring warm water,
  soft tapping, slow massage, fluffy towel patting.
- The character is calm and sleepy-eyed, movements are slow and gentle,
  the palette is pastel, the light is warm, the space is cozy.
- No dialogue, no narration, no background music, no external ambience.
  The native audio of each clip IS the star — it already matches the
  action (brushing sounds like brushing, pouring sounds like pouring).

## Plan the episode

- Build episodes of 1 to 2 minutes, 6 to 12 scenes.
- Pick one service and one cat character to carry the whole episode.
- Write the script as YAML with `title`, `kind: asmr`, `aspect: '16:9'`,
  and scenes carrying `title`, `duration`, `visual`, and `sfx`.
- Give every scene one gentle action and one soft sound, written in the
  `sfx` field so the prompt carries it.
- Keep the example script at `templates/asmr_script.yaml` as the shape
  to copy (Mimi's Cat Spa, 60s, 6 scenes), and keep new scripts under
  `scripts/`.

## Write the script

- Open with the cozy space, then one gentle service step per scene.
- Describe the action slowly and concretely: what the paws do, how the
  water pours, how the towel pats.
- Name the soft sound of each action in the `sfx` field (for example
  "soft rhythmic brushing, gentle bristle sounds").
- End on a peaceful relaxing shot: the customer resting with eyes closed,
  a small flower placed beside them, warm golden light.
- Keep the character table in the script (`characters:`) with one fixed
  English description per cat, so the character looks the same in every
  scene.

## Generate the video prompts

- Run `python cli.py prompts --script scripts/<name>.yaml` to print one
  English prompt per scene.
- Add `--account <name>` to have Muse write the prompts through the
  gateway, and add `--apply` to write the prompts into `visual` and save
  `<name>.prompts.yaml` for the build.
- Check that every prompt names the cat and the action, names a close-up
  camera framing (extreme close-up, macro close-up, slow close-up), uses
  cute soft ASMR animation language, and carries the soft sound of the
  action, with no dialogue text.

## Build the video

- Run `python cli.py build --script scripts/<name>.prompts.yaml --no-tts`
  to generate the clips and stitch the final video. ASMR has no narration,
  so always build with `--no-tts` and no ElevenLabs key is needed at all:
  the sound is created by the video generation itself (each clip's native
  audio), not by any TTS service.
- A build needs a Muse account cookie and consumes weekly quota, so run
  `python cli.py build --script <file> --mock --no-tts` first when testing
  the pipeline without spending quota.
- After the build, re-attach the clips' native audio as the background:
  the build pipeline strips the clips' audio when it assembles the final,
  so concatenate the audio tracks from `output/<episode>/clips/*.mp4` in
  scene order and mix them under the final at unity volume. Do NOT add
  background music or an external ambience track — added audio sounds fake.
- Keep the generated clips at 1280x720 even when the script asks for 8
  seconds, because the video model returns 10 second clips, and plan scene
  durations around that.
- If a scene fails to generate, rebuild it in a gentler way without asking
  again: drop the element that caused the failure and keep the story beat.
- Verify the finished file before delivering it, checking exact duration,
  16:9 aspect, and that the native audio is audible in the gaps between
  actions.

## Push and keep the repo straight

- Push only when the user says so, and use the snapshot push-files flow to
  `tunhary754-maker/automuse-multi` on branch `main`.
- Never run a plain `git push`, because the local remote still points at
  the stale `ducpro283` copy.
- Read the sync record at
  `~/workspace/automuse-multi.muse-github-snapshot-sync.json` before
  pushing, verify the remote head still matches it, and append the new
  entry after a confirmed push.
