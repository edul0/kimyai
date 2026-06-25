package com.kemy.app;

import android.Manifest;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

/** App Kemy: um WebView que carrega a Kemy do seu PC (mesma rede Wi-Fi). */
public class MainActivity extends AppCompatActivity {

    private WebView web;
    private SharedPreferences prefs;
    private ValueCallback<Uri[]> filePathCallback;
    private ActivityResultLauncher<Intent> fileChooser;
    private String pendingShare = null;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("kemy", Context.MODE_PRIVATE);

        // permissao de microfone (pra falar com a Kemy pelo navegador)
        try {
            if (ActivityCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                    != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(this, new String[]{Manifest.permission.RECORD_AUDIO}, 1);
            }
        } catch (Exception ignored) {}

        // seletor de arquivos (anexar imagem/pdf/doc no chat)
        fileChooser = registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), result -> {
            if (filePathCallback == null) return;
            Uri[] uris = null;
            if (result.getResultCode() == RESULT_OK && result.getData() != null) {
                Uri u = result.getData().getData();
                if (u != null) uris = new Uri[]{u};
            }
            filePathCallback.onReceiveValue(uris);
            filePathCallback = null;
        });

        FrameLayout root = new FrameLayout(this);
        final SwipeRefreshLayout swipe = new SwipeRefreshLayout(this);
        web = new WebView(this);
        swipe.addView(web);
        root.addView(swipe, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        // botao de engrenagem (mudar o endereco do PC)
        Button gear = new Button(this);
        gear.setText("⚙");
        gear.setTextColor(Color.WHITE);
        gear.setBackgroundColor(0x66000000);
        FrameLayout.LayoutParams glp = new FrameLayout.LayoutParams(140, 140);
        glp.gravity = Gravity.TOP | Gravity.END;
        glp.topMargin = 24; glp.rightMargin = 24;
        gear.setOnClickListener(v -> askUrl());
        root.addView(gear, glp);
        setContentView(root);

        configureWeb();
        swipe.setOnRefreshListener(() -> { web.reload(); swipe.setRefreshing(false); });

        // se abriu via "compartilhar", guarda o texto pra mandar quando carregar
        handleShare(getIntent());

        String url = prefs.getString("url", "");
        if (url.isEmpty()) askUrl();
        else web.loadUrl(url);
    }

    private void configureWeb() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setDatabaseEnabled(true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req != null && req.isForMainFrame()) {
                    Toast.makeText(MainActivity.this,
                            "Não consegui conectar. Confere o endereço e se o PC está na mesma rede.",
                            Toast.LENGTH_LONG).show();
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                runOnUiThread(() -> request.grant(request.getResources()));   // libera mic/camera no WebView
            }
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams params) {
                filePathCallback = cb;
                try {
                    Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                    i.addCategory(Intent.CATEGORY_OPENABLE);
                    i.setType("*/*");
                    fileChooser.launch(Intent.createChooser(i, "Selecionar arquivo"));
                } catch (Exception e) {
                    filePathCallback = null;
                    return false;
                }
                return true;
            }
        });
    }

    private void askUrl() {
        final EditText in = new EditText(this);
        in.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        in.setHint("http://192.168.0.10:8800");
        String cur = prefs.getString("url", "");
        in.setText(cur.isEmpty() ? "http://192.168." : cur);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = 40; box.setPadding(pad, pad, pad, 0);
        box.addView(in);
        new AlertDialog.Builder(this)
                .setTitle("Endereço da Kemy")
                .setMessage("No app do PC, clique em 'Conectar celular' e digite aqui o endereço mostrado.")
                .setView(box)
                .setPositiveButton("Conectar", (d, w) -> {
                    String u = in.getText().toString().trim();
                    if (!u.startsWith("http")) u = "http://" + u;
                    prefs.edit().putString("url", u).apply();
                    web.loadUrl(u);
                })
                .setNegativeButton("Cancelar", null)
                .show();
    }

    private void handleShare(Intent intent) {
        if (intent != null && Intent.ACTION_SEND.equals(intent.getAction())) {
            String txt = intent.getStringExtra(Intent.EXTRA_TEXT);
            if (txt != null) pendingShare = txt;
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleShare(intent);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
