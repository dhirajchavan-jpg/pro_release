import { api } from "./client.js";
import { endpoints } from "../config/api.js";

export const getAccount = () => api.get(endpoints.account);
export const login = (credentials) => api.post(endpoints.login, credentials);
export const register = (details) => api.post(endpoints.register, details);
export const updateProfile = (profile) => api.patch(endpoints.account, profile);
export const logout = () => api.post(endpoints.logout);

export const listUsers = () => api.get(endpoints.users);
export const createUser = (data) => api.post(endpoints.users, data);
export const updateUser = (userId, data) => api.patch(endpoints.user(userId), data);

export const listInvites = () => api.get(endpoints.invites);
export const createInvite = (data) => api.post(endpoints.invites, data);
export const deleteInvite = (inviteId) => api.delete(endpoints.invite(inviteId));

export const verifyInvite = (token) => api.get(endpoints.verifyInvite(token));
export const acceptInvite = (data) => api.post(endpoints.acceptInvite, data);
