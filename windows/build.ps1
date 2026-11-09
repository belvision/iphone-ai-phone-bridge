$ErrorActionPreference = 'Stop'
dotnet publish -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:EnableCompressionInSingleFile=true -o publish
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
