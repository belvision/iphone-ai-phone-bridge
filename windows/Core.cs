using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Data.Sqlite;
using Windows.ApplicationModel.Calls;
using Windows.Devices.Bluetooth;

namespace GaevAssistant;
static class Data {
 public static readonly string Root=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Gaev","Assistant");
 public static string FileAt(string name)=>Path.Combine(Root,name);
 public static JsonObject Secrets(string name)=>JsonNode.Parse(ProtectedData.Unprotect(File.ReadAllBytes(FileAt(name)),null,DataProtectionScope.CurrentUser))!.AsObject();
 public static void SaveSecrets(string name,JsonObject data){var path=FileAt(name);var tmp=path+"."+Guid.NewGuid()+".tmp";try{File.WriteAllBytes(tmp,ProtectedData.Protect(Encoding.UTF8.GetBytes(data.ToJsonString()),null,DataProtectionScope.CurrentUser));File.Move(tmp,path,true);}finally{File.Delete(tmp);}}
 public static string S(JsonObject d,string key)=>d[key]?.ToString()??"";
 public static void Initialize() {
  Directory.CreateDirectory(Root);
  Execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,started TEXT,name TEXT,phone TEXT,conversation TEXT DEFAULT '',ended TEXT DEFAULT '',result TEXT DEFAULT '',transcript TEXT DEFAULT '')");
 }
 public static void Execute(string sql,params (string,object)[] args){using var db=new SqliteConnection("Data Source="+FileAt("assistant.sqlite3"));db.Open();using var cmd=db.CreateCommand();cmd.CommandText=sql;foreach(var (k,v) in args)cmd.Parameters.AddWithValue(k,v);cmd.ExecuteNonQuery();}
 public static System.Data.DataTable Table(string file,string sql){using var db=new SqliteConnection("Data Source="+FileAt(file)+";Mode=ReadOnly");db.Open();using var cmd=db.CreateCommand();cmd.CommandText=sql;using var r=cmd.ExecuteReader();var t=new System.Data.DataTable();t.Load(r);return t;}
 public static IWebProxy? Proxy(){if(!File.Exists(FileAt("network.dpapi")))return null;var p=Secrets("network.dpapi");if(string.IsNullOrWhiteSpace(S(p,"address")))return null;return new WebProxy(S(p,"address")){Credentials=new NetworkCredential(S(p,"username"),S(p,"password"))};}
 public static HttpClient Http(){var h=new HttpClient(new HttpClientHandler{Proxy=Proxy(),UseProxy=true,AllowAutoRedirect=false}){Timeout=TimeSpan.FromSeconds(30)};h.DefaultRequestHeaders.Add("xi-api-key",S(Secrets("eleven.dpapi"),"elevenKey"));return h;}
}
record PhoneSnapshot(string Phase,string Id,string Name,string Number,bool Local,PhoneCall? Call);
static class Phone {
 public static async Task<PhoneSnapshot> Read(CancellationToken ct) {
  var store=await PhoneCallManager.RequestStoreAsync().AsTask(ct).WaitAsync(TimeSpan.FromSeconds(3),ct);
  var id=await store.GetDefaultLineAsync().AsTask(ct).WaitAsync(TimeSpan.FromSeconds(3),ct);
  var line=await PhoneLine.FromIdAsync(id).AsTask(ct).WaitAsync(TimeSpan.FromSeconds(3),ct);
  if(line==null||line.Transport!=PhoneLineTransport.Bluetooth)throw new Exception("Не найдена Bluetooth-линия телефона.");
  var device=await BluetoothDevice.FromBluetoothAddressAsync(line.TransportDeviceId.Length>0?BluetoothAddress(line.TransportDeviceId):0).AsTask(ct).WaitAsync(TimeSpan.FromSeconds(3),ct);
  using(device){if(device==null||device.ConnectionStatus!=BluetoothConnectionStatus.Connected)throw new Exception("Телефон не подключён по Bluetooth.");}
  var result=await line.GetAllActivePhoneCallsAsync().AsTask(ct).WaitAsync(TimeSpan.FromSeconds(3),ct);
  if(result.OperationStatus!=PhoneLineOperationStatus.Succeeded)throw new Exception("Windows не вернула состояние звонка.");
  var calls=result.AllActivePhoneCalls.Where(c=>c.Status!=PhoneCallStatus.Ended&&c.Status!=PhoneCallStatus.Lost).ToArray();
  if(calls.Length==0)return new("idle","","","",false,null);
  if(calls.Length!=1)return new("multiple","","","",false,null);
  var c=calls[0];var info=c.GetPhoneCallInfo();return new(c.Status.ToString(),c.CallId,info.DisplayName??"",info.PhoneNumber??"",c.AudioDevice==PhoneCallAudioDevice.LocalDevice,c);
 }
 static ulong BluetoothAddress(string text){var match=System.Text.RegularExpressions.Regex.Match(text,@"&([0-9A-Fa-f]{12})_");if(!match.Success)throw new Exception("Не удалось определить Bluetooth-устройство.");return Convert.ToUInt64(match.Groups[1].Value,16);}
}
