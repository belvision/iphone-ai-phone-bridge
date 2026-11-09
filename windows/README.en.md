# GAEV Call Assistant

[Русский](README.md)

An experimental Windows 11 call assistant. It monitors an iPhone call through Windows Phone Link and routes audio between the call and an ElevenLabs voice agent using VB-CABLE. Russian-language scripts can be edited in the app. Session history and transcripts are stored locally.

## Quick start

1. Install the .NET 10 SDK and VB-CABLE. Pair an iPhone over Bluetooth with Windows Phone Link and confirm that calls are available there.
2. From the source folder, run `dotnet run --project GaevAssistant.csproj`. For a self-contained Windows x64 build, run `powershell -ExecutionPolicy Bypass -File build.ps1`.
3. Enter your own ElevenLabs API key and configured voice agent ID in Settings. The API key is stored using Windows DPAPI. Review and apply a script on the Scenarios tab.
4. Enable standby. Once the assistant shows it is ready, place a test call through Phone Link and check both audio directions.

Phone and audio support depends on the handset, Bluetooth adapter and Windows drivers. Inform participants when a voice AI joins a call. ElevenLabs processes the transmitted audio, so review your agent and retention settings before use.

This repository contains the general-purpose source release. It does not contain contact data, call history, credentials or the private GAEV chat integration.
