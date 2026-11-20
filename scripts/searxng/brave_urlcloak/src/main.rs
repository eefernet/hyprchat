//! Offline cryptography only: no sockets, browser code, files or query logging.
//! Uses Brave's RFC 9497 implementation; the caller owns bounded VPN transport.
use aes_gcm::{Aes256Gcm, KeyInit, Nonce, aead::Aead};
use base64::{Engine, engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD}};
use ed25519_dalek::{Signature, VerifyingKey};
use poprf_ristretto::{BlindedElement, EvaluatedElement, PoprfBlindState, PoprfClient, Proof, PublicKey};
use rand_core::OsRng;
use serde_json::{Value, json};
use std::io::{self, Read};

type Result<T> = std::result::Result<T, &'static str>;
const MAX_INPUT: u64 = 512 * 1024;
const MAX_TOKENS: usize = 64;

fn string<'a>(value: &'a Value, name: &str) -> Result<&'a str> {
    value.get(name).and_then(Value::as_str).ok_or("invalid input")
}
fn decode(value: &str) -> Result<Vec<u8>> {
    STANDARD.decode(value).map_err(|_| "invalid base64")
}
struct Token {
    bytes: Vec<u8>,
    sid: String,
    index: u8,
    https: bool,
}
fn token(raw: &str, key: &Value, now: u64) -> Result<Token> {
    if raw.len() > 8192 || raw.len() < 144 { return Err("invalid token length"); }
    let bytes = URL_SAFE_NO_PAD.decode(raw).map_err(|_| "invalid token")?;
    if bytes.len() <= 31 + 12 + 64 || bytes[0] != 1 || bytes[30] & 64 != 0 {
        return Err("unsupported token format");
    }
    if key.get("version").and_then(Value::as_u64) != Some(bytes[13] as u64) {
        return Err("key version mismatch");
    }
    let issued = u32::from_be_bytes(bytes[9..13].try_into().unwrap()) as u64;
    if issued > now.saturating_add(300) || now.saturating_sub(issued) > 604800 {
        return Err("expired token");
    }
    let public: [u8; 32] = decode(string(key, "sig_public_key")?)?.try_into().map_err(|_| "invalid signing key")?;
    let verifier = VerifyingKey::from_bytes(&public).map_err(|_| "invalid signing key")?;
    let split = bytes.len() - 64;
    let signature = Signature::from_slice(&bytes[split..]).map_err(|_| "invalid signature")?;
    let mut signed = b"urlcloak-ct-v1".to_vec();
    signed.extend_from_slice(&bytes[..split]);
    verifier.verify_strict(&signed, &signature).map_err(|_| "invalid signature")?;
    Ok(Token { sid: URL_SAFE_NO_PAD.encode(&bytes[..30]), index: bytes[30] & 63,
               https: bytes[30] & 128 != 0, bytes })
}
fn context(token: &Token) -> (Vec<u8>, Vec<u8>) {
    (token.index.to_string().into_bytes(), format!(r#"{{"d":"urlkey","sid":"{}"}}"#, token.sid).into_bytes())
}
fn client(key: &Value) -> Result<PoprfClient> {
    let public = PublicKey::from_bytes(&decode(string(key, "public_key")?)?).map_err(|_| "invalid public key")?;
    Ok(PoprfClient::new(public))
}
fn prepare(raw: &str, key: &Value, now: u64) -> Result<Value> {
    let token = token(raw, key, now)?;
    let (input, info) = context(&token);
    let (state, blinded) = client(key)?.blind(&input, &info, &mut OsRng).map_err(|_| "blinding failed")?;
    Ok(json!({"token":raw,"sid":token.sid,"state":STANDARD.encode(state.to_bytes()),
              "blinded_element":STANDARD.encode(blinded.to_bytes())}))
}
fn finish(item: &Value, key: &Value, now: u64) -> Result<String> {
    let token = token(string(item, "token")?, key, now)?;
    let (input, info) = context(&token);
    let state = PoprfBlindState::from_bytes(&decode(string(item, "state")?)?).map_err(|_| "invalid state")?;
    let blinded = BlindedElement::from_bytes(&decode(string(item, "blinded_element")?)?).map_err(|_| "invalid blinded element")?;
    let evaluated = EvaluatedElement::from_bytes(&decode(string(item, "evaluated_element")?)?).map_err(|_| "invalid evaluated element")?;
    let proof = Proof::from_bytes(&decode(string(item, "proof")?)?).map_err(|_| "invalid proof")?;
    let output = client(key)?.finalize(&input, &state, &evaluated, &blinded, &proof, &info).map_err(|_| "proof verification failed")?;
    let cipher = Aes256Gcm::new_from_slice(&output.as_bytes()[..32]).map_err(|_| "invalid decryption key")?;
    let end = token.bytes.len() - 64;
    let plain = cipher.decrypt(Nonce::from_slice(&token.bytes[31..43]), &token.bytes[43..end]).map_err(|_| "decryption failed")?;
    let target = String::from_utf8(plain).map_err(|_| "invalid destination")?;
    if target.is_empty() || target.len() > 4096 || target.chars().any(char::is_control) {
        return Err("invalid destination");
    }
    Ok(format!("{}://{}", if token.https { "https" } else { "http" }, target))
}
fn execute(request: &Value) -> Result<Value> {
    let key = request.get("key").ok_or("missing key")?;
    let now = request.get("now").and_then(Value::as_u64).ok_or("missing time")?;
    let items = request.get("items").and_then(Value::as_array).ok_or("missing items")?;
    if items.is_empty() || items.len() > MAX_TOKENS { return Err("invalid item count"); }
    let operation = string(request, "op")?;
    let result: Result<Vec<Value>> = items.iter().map(|item| match operation {
        "prepare" => prepare(item.as_str().ok_or("invalid token")?, key, now),
        "finish" => Ok(json!(finish(item, key, now)?)),
        _ => Err("unsupported operation"),
    }).collect();
    Ok(json!({"items":result?}))
}
fn main() {
    let mut bytes = Vec::new();
    let result = io::stdin().take(MAX_INPUT + 1).read_to_end(&mut bytes)
        .map_err(|_| "input read failed").and_then(|_| {
            if bytes.len() as u64 > MAX_INPUT { return Err("input too large"); }
            let request: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid JSON")?;
            execute(&request)
        });
    match result {
        Ok(value) => println!("{}", value),
        Err(error) => { eprintln!("{}", error); std::process::exit(1); }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};
    use poprf_ristretto::PoprfServer;
    const NOW: u64 = 1795124000;
    fn fixture(index: u8, https: bool) -> (String, Value, PoprfServer) {
        let server = PoprfServer::generate(&mut OsRng);
        let signer = SigningKey::generate(&mut OsRng);
        let key = json!({"version":1,"public_key":STANDARD.encode(server.public_key().to_bytes()),
                         "sig_public_key":STANDARD.encode(signer.verifying_key().as_bytes())});
        let mut bytes = vec![0u8;30];bytes[0]=1;bytes[1]=7;bytes[13]=1;
        bytes[9..13].copy_from_slice(&(NOW as u32).to_be_bytes());
        let sid = URL_SAFE_NO_PAD.encode(&bytes);
        bytes.push(index | if https {128} else {0});bytes.extend_from_slice(&[8u8;12]);
        let info=format!(r#"{{"d":"urlkey","sid":"{}"}}"#,sid);
        let output=server.evaluate(index.to_string().as_bytes(), info.as_bytes()).unwrap();
        let cipher=Aes256Gcm::new_from_slice(&output.as_bytes()[..32]).unwrap();
        bytes.extend(cipher.encrypt(Nonce::from_slice(&[8u8;12]), b"docs.python.org/3/library/?q=one%20two#part".as_ref()).unwrap());
        let mut signed=b"urlcloak-ct-v1".to_vec();signed.extend_from_slice(&bytes);
        bytes.extend_from_slice(&signer.sign(&signed).to_bytes());
        (URL_SAFE_NO_PAD.encode(bytes),key,server)
    }
    fn evaluate(item:&mut Value,server:&PoprfServer) {
        let info=format!(r#"{{"d":"urlkey","sid":"{}"}}"#,item["sid"].as_str().unwrap());
        let blinded=BlindedElement::from_bytes(&decode(item["blinded_element"].as_str().unwrap()).unwrap()).unwrap();
        let (evaluated,proof)=server.blind_evaluate(&mut OsRng,&blinded,info.as_bytes()).unwrap();
        item["evaluated_element"]=json!(STANDARD.encode(evaluated.to_bytes()));
        item["proof"]=json!(STANDARD.encode(proof.to_bytes()));
    }
    #[test] fn round_trip_exact_destination() {
        for index in [0,1,63] {for https in [false,true] {
            let (raw,key,server)=fixture(index,https);let mut p=prepare(&raw,&key,NOW).unwrap();
            evaluate(&mut p,&server);
            assert_eq!(finish(&p,&key,NOW).unwrap(),format!("{}://docs.python.org/3/library/?q=one%20two#part",if https {"https"}else{"http"}));
        }}
    }
    #[test] fn blinding_is_fresh() {
        let (raw,key,_)=fixture(0,true);
        assert_ne!(prepare(&raw,&key,NOW).unwrap()["blinded_element"],prepare(&raw,&key,NOW).unwrap()["blinded_element"]);
    }
    #[test] fn modified_token_rejected_before_network() {
        let (raw,key,_)=fixture(0,true);let mut bytes=URL_SAFE_NO_PAD.decode(raw).unwrap();bytes[45]^=1;
        assert!(prepare(&URL_SAFE_NO_PAD.encode(bytes),&key,NOW).is_err());
    }
    #[test] fn invalid_proof_cannot_produce_a_url() {
        let (raw,key,server)=fixture(0,true);let mut p=prepare(&raw,&key,NOW).unwrap();evaluate(&mut p,&server);
        p["proof"]=json!(STANDARD.encode([0u8;64]));assert!(finish(&p,&key,NOW).is_err());
    }
    #[test] fn wrong_version_and_expired_tokens_rejected() {
        let (raw,mut key,_)=fixture(0,true);
        assert!(prepare(&raw,&key,NOW+604801).is_err());assert!(prepare(&raw,&key,NOW-301).is_err());
        key["version"]=json!(2);assert!(prepare(&raw,&key,NOW).is_err());
    }
    #[test] fn bounded_and_unknown_inputs_rejected() {
        let (raw,key,_)=fixture(0,true);
        assert!(prepare("bad",&key,NOW).is_err());
        assert!(execute(&json!({"op":"prepare","key":key,"now":NOW,"items":vec![raw;65]})).is_err());
    }
}
