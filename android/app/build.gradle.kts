import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Release-Schluessel: android/keystore.properties (nicht im Git, siehe README). Fehlt sie, entsteht eine
// unsignierte APK, wie F-Droid sie selbst baut und signiert.
val keystore = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "io.github.mainbrain_mk.bbq"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.mainbrain_mk.bbq"
        minSdk = 26
        targetSdk = 36
        versionCode = 100 // = versionName × 100: 1.0 -> 100, 1.01 -> 101, 1.1 -> 110
        versionName = "1.0"
    }
    signingConfigs {
        if (keystore.isNotEmpty()) create("release") {
            storeFile = rootProject.file(keystore.getProperty("storeFile"))
            storePassword = keystore.getProperty("storePassword")
            keyAlias = keystore.getProperty("keyAlias")
            keyPassword = keystore.getProperty("keyPassword")
        }
    }
    buildTypes {
        release {
            signingConfig = signingConfigs.findByName("release")
        }
    }
    // kein von Google verschluesselter Abhaengigkeits-Block in der APK: F-Droid lehnt ihn ab
    dependenciesInfo {
        includeInApk = false
        includeInBundle = false
    }
    // Sprachliste fuer "App-Sprache" in den Android-Einstellungen (ab Android 13), aus den values-*-Ordnern
    androidResources { generateLocaleConfig = true }
    buildFeatures { compose = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin { jvmToolchain(17) }

dependencies {
    implementation(platform("androidx.compose:compose-bom:2025.12.01"))
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.activity:activity-compose:1.11.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.4")
    implementation("androidx.core:core-ktx:1.17.0")
    testImplementation("junit:junit:4.13.2")
}
