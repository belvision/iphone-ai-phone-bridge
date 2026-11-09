using System.Diagnostics;
using System.Net;
using System.Text.Json.Nodes;
namespace GaevAssistant;
sealed class ConnectionPreparation:IDisposable {
 public IWebProxy? Proxy {get;}
 readonly HttpClient http;readonly string agent;
 Uri? cached;DateTime expires;
 public ConnectionPreparation(JsonObject config){Proxy=Data.Proxy();agent=Data.S(config,"agentId");http=new HttpClient(new HttpClientHandler{UseProxy=true,Proxy=Proxy,AllowAutoRedirect=false}){Timeout=TimeSpan.FromSeconds(20)};http.DefaultRequestHeaders.Add("xi-api-key",Data.S(config,"elevenKey"));}
 public async Task Prepare(CancellationToken ct){if(cached!=null&&DateTime.UtcNow<expires)return;using var r=await http.GetAsync("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id="+Uri.EscapeDataString(agent),ct);r.EnsureSuccessStatusCode();var data=JsonNode.Parse(await r.Content.ReadAsStringAsync(ct));var uri=new Uri(data!["signed_url"]!.ToString());if(uri.Scheme!="wss"||uri.Host!="api.elevenlabs.io"||uri.AbsolutePath!="/v1/convai/conversation")throw new Exception("Некорректный адрес ElevenLabs.");cached=uri;expires=DateTime.UtcNow.AddMinutes(10);}
 public async Task<Uri> Take(CancellationToken ct){await Prepare(ct);var result=cached!;cached=null;return result;}
 public void Dispose()=>http.Dispose();
}
