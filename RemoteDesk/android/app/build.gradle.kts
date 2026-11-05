plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.remotedesk.android"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.remotedesk.android"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    packaging {
        resources {
            excludes += setOf("META-INF/versions/9/OSGI-INF/MANIFEST.MF", "META-INF/*.SF",
                "META-INF/*.DSA", "META-INF/*.RSA")
        }
    }
}

dependencies {
    // X25519 key agreement (Android's own XDH needs API 33+)
    implementation("org.bouncycastle:bcprov-jdk18on:1.78.1")
}
