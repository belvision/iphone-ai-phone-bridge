using NAudio.CoreAudioApi;
namespace GaevAssistant;
static class SelfTest {
 public static async Task Run(){
  Data.Initialize();ContactStore.Import();var report=new List<string>();
  var p=await Phone.Read(CancellationToken.None);if(p.Phase!="idle")throw new Exception("Self-test requires idle phone");report.Add("PASS: native Windows phone API (idle)");
  if(Data.Table("assistant.sqlite3","SELECT id FROM contacts").Rows.Count!=460)throw new Exception("Contact migration mismatch");report.Add("PASS: 460 local contacts");
  string[] Defaults(){using var en=new MMDeviceEnumerator();return new[]{Role.Console,Role.Multimedia,Role.Communications}.Select(r=>{using var d=en.GetDefaultAudioEndpoint(DataFlow.Capture,r);return d.ID;}).ToArray();}
  var original=Defaults();using(var lease=new MicLease()){using var cable=MicLease.Cable(DataFlow.Capture);if(Defaults().Any(x=>x!=cable.ID))throw new Exception("Microphone route mismatch");using var bridge=new AudioBridge(16000,16000);bridge.Play(new byte[3200]);var frame=await bridge.Frames.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(3));if(frame.Length!=3200)throw new Exception("PCM packet size mismatch");await Task.Delay(150);}
  if(!Defaults().SequenceEqual(original))throw new Exception("Microphone restoration mismatch");report.Add("PASS: native WASAPI open, silent PCM playback, microphone restoration");
  for(int i=0;i<3;i++){var engine=new Engine();engine.Start();if(i==0){var until=DateTime.UtcNow.AddSeconds(30);while(engine.Phase!=RunPhase.Ready&&engine.Phase!=RunPhase.Error&&DateTime.UtcNow<until)await Task.Delay(100);if(engine.Phase!=RunPhase.Ready){await engine.Stop();throw new Exception("Preparation did not reach Ready");}}else await Task.Delay(150);await engine.Stop();if(engine.Enabled)throw new Exception("Cancellation failed");}report.Add("PASS: repeated start/cancel without dialing");
  if(!Defaults().SequenceEqual(original))throw new Exception("Defaults changed after cancellation");
  using var http=Data.Http();var cfg=Data.Secrets("eleven.dpapi");using var response=await http.GetAsync("https://api.elevenlabs.io/v1/convai/agents/"+Uri.EscapeDataString(Data.S(cfg,"agentId")));response.EnsureSuccessStatusCode();report.Add("PASS: direct application proxy API request (200)");
  var old=Data.Table("crm.sqlite3","SELECT conversation_id FROM calls WHERE conversation_id<>'' AND ended_at IS NOT NULL ORDER BY started_at DESC LIMIT 1");if(old.Rows.Count>0){var conv=old.Rows[0][0].ToString()!;var bytes=await http.GetByteArrayAsync("https://api.elevenlabs.io/v1/convai/conversations/"+Uri.EscapeDataString(conv)+"/audio");using var stream=new MemoryStream(bytes);using var mp3=new NAudio.Wave.Mp3FileReader(stream);var pcm=new byte[4096];if(mp3.Read(pcm,0,pcm.Length)<=0)throw new Exception("Empty recording");report.Add("PASS: completed recording downloaded through proxy and decoded locally (no audible playback)");}
  File.WriteAllLines(Data.FileAt("self-test.txt"),report);
 }
}
