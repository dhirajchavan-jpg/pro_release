import { api } from "./client.js";
import { endpoints } from "../config/api.js";

export const getAccount = () => api.get(endpoints.account);
export const login = (credentials) => api.post(endpoints.login, credentials);
export const register = (details) => api.post(endpoints.register, details);
export const updateProfile = (profile) => api.patch(endpoints.account, profile);
export const logout = () => api.post(endpoints.logout);
